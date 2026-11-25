"""Versioned SQLite upgrades. Only transactional SQL belongs in a migration.

Backups use SQLite's online backup API (including committed WAL contents), not
a filesystem copy. Original uploads are BLOBs in this development database.
No backup is automatically deleted, even when recovery fails.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

DB_NAME = 'development.sqlite3'


class MigrationError(RuntimeError):
    def __init__(self, stage, cause, backup=None, recovery=None, recovery_error=None):
        self.stage, self.cause, self.backup = stage, cause, backup
        self.recovery, self.recovery_error = recovery, recovery_error
        super().__init__(f'DB 작업 중단 [{stage}]: {cause}; backup={backup}; '
                         f'recovery={recovery}; recovery_error={recovery_error}')


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple

    @property
    def checksum(self):
        return hashlib.sha256('\n'.join(self.statements).encode()).hexdigest()


V1 = Migration(1, 'initial_development_schema', (
    'CREATE TABLE users(name TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL)',
    'CREATE TABLE sessions(token TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(name), expires REAL NOT NULL)',
    '''CREATE TABLE documents(id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL,
       original BLOB NOT NULL, sha256 TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
       draft TEXT NOT NULL DEFAULT '{"sales":[]}', created REAL NOT NULL)''',
    '''CREATE TABLE revisions(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
       version INTEGER NOT NULL, payload TEXT NOT NULL, results TEXT NOT NULL,
       username TEXT NOT NULL REFERENCES users(name), created REAL NOT NULL, UNIQUE(document_id,version))''',
    '''CREATE TABLE audit(id INTEGER PRIMARY KEY, username TEXT NOT NULL, action TEXT NOT NULL,
       document_id TEXT, version INTEGER, created REAL NOT NULL)''',
))
V2 = Migration(2, 'migration_history_and_lookup_indexes', (
    '''CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL,
       checksum TEXT NOT NULL, applied_at TEXT NOT NULL)''',
    'CREATE INDEX idx_documents_created ON documents(created)',
    'CREATE INDEX idx_sessions_expires ON sessions(expires)',
    'CREATE INDEX idx_audit_document ON audit(document_id, version)',
))
V3 = Migration(3, 'immutable_sales_import_and_review', (
    '''CREATE TABLE sales_imports(id TEXT PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
       original BLOB NOT NULL, headers TEXT NOT NULL, created REAL NOT NULL,
       username TEXT NOT NULL REFERENCES users(name))''',
    '''CREATE TABLE sales_rows(id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES sales_imports(id),
       sheet TEXT NOT NULL, row_number INTEGER NOT NULL, raw TEXT NOT NULL, summary TEXT NOT NULL,
       version INTEGER NOT NULL DEFAULT 1, draft TEXT NOT NULL,
       UNIQUE(import_id,sheet,row_number))''',
    '''CREATE TABLE sales_reviews(id INTEGER PRIMARY KEY, row_id TEXT NOT NULL REFERENCES sales_rows(id),
       version INTEGER NOT NULL, draft TEXT NOT NULL, result TEXT NOT NULL,
       username TEXT NOT NULL REFERENCES users(name), created REAL NOT NULL, UNIQUE(row_id,version))''',
    'CREATE INDEX idx_sales_rows_import ON sales_rows(import_id,row_number)',
))
MIGRATIONS = (V1, V2, V3)
LATEST_VERSION = MIGRATIONS[-1].version


@contextmanager
def file_lock(root, name):
    """OS lock: crash releases it; never terminate another process or delete locks."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / name, 'a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise MigrationError('lock', '사용 중인 서버/DB 작업을 먼저 정상 종료하세요.') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def version(db):
    return db.execute('PRAGMA user_version').fetchone()[0]


def read_only(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def integrity(db):
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('DB 무결성 검사 실패')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('DB 외래 키 검사 실패')


def structure(db, table):
    columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
    foreign = sorted(db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall())
    indexes = []
    for row in db.execute(f'PRAGMA index_list("{table}")').fetchall():
        cols = tuple(x[2] for x in db.execute(f'PRAGMA index_info("{row[1]}")'))
        indexes.append((row[2], row[4], cols))
    return columns, foreign, sorted(indexes)


def validate_schema(db, number, migrations=MIGRATIONS):
    """Validate columns, keys, required indexes, history, not just a version flag."""
    integrity(db)
    if number == 0:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
            raise ValueError('버전 없는 기존 DB는 자동 추정하지 않습니다.')
        return
    if not 1 <= number <= migrations[-1].version:
        raise ValueError('이 프로그램보다 새로운 DB이거나 지원하지 않는 버전입니다.')
    with closing(sqlite3.connect(':memory:')) as expected:
        for migration in migrations:
            if migration.version <= number:
                for sql in migration.statements:
                    expected.execute(sql)
        for (table,) in expected.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            actual, ref = structure(db, table), structure(expected, table)
            if actual[:2] != ref[:2] or not all(x in actual[2] for x in ref[2]):
                raise ValueError(f'DB 구조가 버전 계약과 다릅니다: {table}')
    if number >= 2:
        rows = db.execute('SELECT version,name,checksum FROM schema_migrations ORDER BY version').fetchall()
        wanted = [(m.version, m.name, m.checksum) for m in migrations if m.version <= number]
        if rows != wanted:
            raise ValueError('마이그레이션 이력/체크섬이 일치하지 않습니다.')


def _copy_database(source, target):
    deadline = time.monotonic() + 15
    def progress(status, remaining, total):
        nonlocal deadline
        if status in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
            deadline = time.monotonic() + 15
        if time.monotonic() > deadline:
            raise TimeoutError('DB 백업/복원 제한시간 초과')
    source.backup(target, pages=256, progress=progress, sleep=0.05)


def backup_database(path, label, migrations=MIGRATIONS):
    directory = Path(path).parent / 'backups'
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = directory / f'{stamp}-{label}-{uuid4().hex}.sqlite3'
    # Unique destination, no reuse or overwrite of a prior backup.
    try:
        with closing(read_only(path)) as source, closing(sqlite3.connect(target)) as dest:
            _copy_database(source, dest)
            validate_schema(dest, version(source), migrations)
    except Exception as exc:
        raise MigrationError('backup', exc, target) from exc
    return target


def _execute_step(db, migration):
    # Prevent future migration authors from accidentally committing midway,
    # changing user_version themselves, attaching databases or using executescript.
    def authorize(action, arg1, arg2, database, trigger):
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                      sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_PRAGMA):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(authorize)
    try:
        for sql in migration.statements:
            db.execute(sql)
    finally:
        db.set_authorizer(None)


def migrate(root, migrations=MIGRATIONS):
    root = Path(root)
    with file_lock(root, '.db-maintenance.lock'):
        path = root / DB_NAME
        backup = None
        original = None
        stage = 'validate'
        with closing(sqlite3.connect(path, timeout=5, isolation_level=None)) as db:
            try:
                db.execute('PRAGMA foreign_keys=ON')
                if version(db) == 0 and not db.execute("SELECT 1 FROM sqlite_master").fetchone():
                    db.execute('PRAGMA journal_mode=WAL')
                db.execute('BEGIN IMMEDIATE')  # Exclude writes also by legacy clients.
                original = version(db)
                validate_schema(db, original, migrations)
                if original == migrations[-1].version:
                    db.rollback()
                    return {'from': original, 'to': original, 'backup': None}
                if original:
                    stage = 'backup'
                    # Separate read connection while the writer lock keeps contents stable.
                    backup = backup_database(path, f'pre-v{original}-to-v{migrations[-1].version}', migrations)
                for step in migrations:
                    if step.version <= original:
                        continue
                    if step.version != version(db) + 1:
                        raise ValueError('연속되지 않은 마이그레이션 버전')
                    stage = f'migrate-v{step.version}'
                    _execute_step(db, step)
                    if step.version == 2:
                        db.execute('INSERT INTO schema_migrations VALUES(?,?,?,?)',
                                   (1, migrations[0].name, migrations[0].checksum, datetime.now(timezone.utc).isoformat()))
                    if step.version >= 2:
                        db.execute('INSERT INTO schema_migrations VALUES(?,?,?,?)',
                                   (step.version, step.name, step.checksum, datetime.now(timezone.utc).isoformat()))
                    db.execute(f'PRAGMA user_version={step.version}')
                    validate_schema(db, step.version, migrations)
                stage = 'commit'
                db.commit()
            except Exception as exc:
                recovery_error = None
                recovered = False
                try:
                    db.rollback()
                    if original is not None:
                        # Recovery is only declared when the original version/schema validates.
                        if version(db) != original:
                            raise ValueError('원래 DB 버전으로 돌아오지 않았습니다.')
                        validate_schema(db, original, migrations)
                        recovered = True
                except Exception as rollback_exc:
                    recovery_error = rollback_exc
                raise MigrationError(stage, exc, backup or getattr(exc, 'backup', None), recovered, recovery_error) from exc
        return {'from': original, 'to': migrations[-1].version, 'backup': str(backup) if backup else None}


def restore(root, source_path):
    try:
        return _restore(root, source_path)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError('restore-prepare', exc) from exc


def _restore(root, source_path):
    """Explicit offline recovery. Preserve the current DB before restoring anything.

    The server holds .runtime.lock for its lifetime. Never delete WAL/SHM files or
    copy a raw database over an open one. Backup API replacement is transactional.
    """
    root, source_path = Path(root).resolve(), Path(source_path).resolve()
    path = root / DB_NAME
    if source_path == path or not source_path.is_file():
        raise MigrationError('restore-validate', '다른 백업 DB 파일을 지정하세요.')
    with file_lock(root, '.runtime.lock'), file_lock(root, '.db-maintenance.lock'):
        # A read transaction pins the validated snapshot without loading BLOBs into RAM.
        with closing(read_only(source_path)) as source:
            source.execute('BEGIN')
            number = version(source)
            validate_schema(source, number)
            if number == 0:
                raise MigrationError('restore-validate', '빈 DB는 복원할 수 없습니다.')
            pre_restore = backup_database(path, 'pre-restore') if path.exists() else None
            try:
                with closing(sqlite3.connect(path, timeout=5)) as dest:
                    _copy_database(source, dest)
                    validate_schema(dest, number)
            except Exception as exc:
                recovered, recovery_error = False, None
                try:
                    if pre_restore:
                        with closing(read_only(pre_restore)) as previous, closing(sqlite3.connect(path)) as dest:
                            _copy_database(previous, dest)
                            validate_schema(dest, version(previous))
                            recovered = True
                except Exception as rollback_exc:
                    recovery_error = rollback_exc
                raise MigrationError('restore', exc, pre_restore, recovered, recovery_error) from exc
            return {'restoredVersion': number, 'source': str(source_path),
                    'preRestoreBackup': str(pre_restore) if pre_restore else None}
