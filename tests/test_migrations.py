"""Legacy schema fixture is independent of production migration declarations."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrations as m
from store import Store
from test_core import sale

LEGACY = '''
PRAGMA journal_mode=WAL;
CREATE TABLE users(name TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL);
CREATE TABLE sessions(token TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(name), expires REAL NOT NULL);
CREATE TABLE documents(id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL,
 original BLOB NOT NULL, sha256 TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 draft TEXT NOT NULL DEFAULT '{"sales":[]}', created REAL NOT NULL);
CREATE TABLE revisions(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 version INTEGER NOT NULL, payload TEXT NOT NULL, results TEXT NOT NULL, username TEXT NOT NULL REFERENCES users(name),
 created REAL NOT NULL, UNIQUE(document_id,version));
CREATE TABLE audit(id INTEGER PRIMARY KEY, username TEXT NOT NULL, action TEXT NOT NULL,
 document_id TEXT, version INTEGER, created REAL NOT NULL);
PRAGMA user_version=1;
'''
TABLES = ['users', 'sessions', 'documents', 'revisions', 'audit']


def snapshot(path):
    with closing(m.read_only(path)) as db:
        return {t: db.execute(f'SELECT * FROM {t} ORDER BY rowid').fetchall() for t in TABLES}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / m.DB_NAME
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(LEGACY)
            salt = b'legacy-salt-1234'
            digest = hashlib.pbkdf2_hmac('sha256', b'legacy-password-123', salt, 600000)
            db.execute('INSERT INTO users VALUES(?,?,?)', ('legacy', salt, digest))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(b'existing-token').hexdigest(),'legacy',4102444800))
            original = b'%PDF-1.7 original fixture\x00\xff'
            s = sale(); s['decision'] = 'accepted'
            draft = json.dumps({'sales':[s]}, ensure_ascii=False)
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',
                       ('doc-1','보존 문서.pdf','application/pdf',original,hashlib.sha256(original).hexdigest(),7,draft,123.0))
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?)',('rev-1','doc-1',6,draft,'[]','legacy',122.0))
            db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?)',(1,'legacy','confirm','doc-1',6,122.0))
            db.commit()
        self.before = snapshot(self.path)

    def test_v1_upgrade_preserves_all_rows_blobs_passwords_sessions(self):
        store = Store(self.root)
        self.assertEqual(self.before, snapshot(self.path))
        self.assertEqual(self.before, snapshot(store.migration_result['backup']))
        self.assertEqual('legacy', store.user('existing-token'))
        self.assertIsNotNone(store.login('legacy','legacy-password-123'))
        self.assertEqual([6], store.get('doc-1')['confirmedVersions'])
        self.assertEqual(7, store.get('doc-1')['version'])
        with store.connect() as db:
            self.assertEqual(m.LATEST_VERSION, m.version(db))
            self.assertEqual(list(range(1,m.LATEST_VERSION+1)), [x[0] for x in db.execute('SELECT version FROM schema_migrations ORDER BY version')])

    def test_wal_committed_data_in_backup(self):
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("UPDATE documents SET name='WAL 변경' WHERE id='doc-1'")
            writer.commit()
            self.assertTrue(Path(str(self.path)+'-wal').exists())
            before = snapshot(self.path)
            result = m.migrate(self.root)
            self.assertEqual(before, snapshot(result['backup']))
            self.assertEqual(before, snapshot(self.path))

    def test_repeated_start_does_not_backup_or_rerun(self):
        m.migrate(self.root)
        count = len(list((self.root/'backups').glob('*.sqlite3')))
        result = m.migrate(self.root)
        self.assertIsNone(result['backup'])
        self.assertEqual(count, len(list((self.root/'backups').glob('*.sqlite3'))))

    def test_empty_database_created_at_latest(self):
        other = self.root/'fresh'
        result = m.migrate(other)
        self.assertEqual((0,m.LATEST_VERSION,None),(result['from'],result['to'],result['backup']))
        with closing(m.read_only(other/m.DB_NAME)) as db:m.validate_schema(db,m.LATEST_VERSION)

    def test_partial_multi_step_failure_rolls_back_all_and_retains_backup(self):
        broken = m.Migration(m.LATEST_VERSION+1,'broken',('ALTER TABLE documents ADD COLUMN temporary TEXT',
                                       "UPDATE documents SET name='should roll back'",'INSERT INTO missing_table VALUES(1)'))
        with self.assertRaises(m.MigrationError) as caught:
            m.migrate(self.root,m.MIGRATIONS+(broken,))
        self.assertTrue(caught.exception.recovery)
        self.assertEqual(f'migrate-v{m.LATEST_VERSION+1}',caught.exception.stage)
        self.assertEqual(self.before,snapshot(self.path))
        self.assertEqual(self.before,snapshot(caught.exception.backup))
        with closing(m.read_only(self.path)) as db:
            self.assertEqual(1,m.version(db))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchone())

    def test_backup_failure_prevents_any_schema_change(self):
        with patch.object(m,'_copy_database',side_effect=OSError('disk full')):
            with self.assertRaises(m.MigrationError) as caught:m.migrate(self.root)
        self.assertEqual('backup',caught.exception.stage)
        self.assertEqual(self.before,snapshot(self.path))
        self.assertTrue(caught.exception.recovery)

    def test_unexpected_commit_in_migration_blocked(self):
        bad = m.Migration(m.LATEST_VERSION+1,'bad',("UPDATE documents SET name='bad'",'COMMIT'))
        with self.assertRaises(m.MigrationError):m.migrate(self.root,m.MIGRATIONS+(bad,))
        self.assertEqual(self.before,snapshot(self.path))

    def test_newer_version_never_downgraded(self):
        with closing(sqlite3.connect(self.path)) as db:db.execute('PRAGMA user_version=99')
        with self.assertRaises(m.MigrationError):m.migrate(self.root)
        with closing(m.read_only(self.path)) as db:self.assertEqual(99,m.version(db))
        self.assertEqual(self.before,snapshot(self.path))

    def test_schema_drift_rejected_before_changes(self):
        with closing(sqlite3.connect(self.path)) as db:db.execute('ALTER TABLE documents DROP COLUMN mime')
        with self.assertRaises(m.MigrationError) as caught:m.migrate(self.root)
        self.assertEqual('validate',caught.exception.stage)
        self.assertFalse((self.root/'backups').exists())

    def test_missing_required_unique_index_rejected(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('ALTER TABLE revisions RENAME TO old_revisions; '
                'CREATE TABLE revisions(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), '
                'version INTEGER NOT NULL, payload TEXT NOT NULL, results TEXT NOT NULL, '
                'username TEXT NOT NULL REFERENCES users(name), created REAL NOT NULL); '
                'INSERT INTO revisions SELECT * FROM old_revisions; DROP TABLE old_revisions;')
        with self.assertRaises(m.MigrationError):m.migrate(self.root)

    def test_history_tampering_rejected(self):
        m.migrate(self.root)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE schema_migrations SET checksum='bad' WHERE version=2");db.commit()
        with self.assertRaises(m.MigrationError):m.migrate(self.root)

    def test_restore_keeps_pre_restore_backup_and_reupgrade_works(self):
        result = m.migrate(self.root)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE documents SET name='after upgrade'");db.commit()
        after = snapshot(self.path)
        restored = m.restore(self.root,result['backup'])
        self.assertEqual(1,restored['restoredVersion'])
        self.assertEqual(self.before,snapshot(self.path))
        self.assertEqual(after,snapshot(restored['preRestoreBackup']))
        Store(self.root)
        self.assertEqual(self.before,snapshot(self.path))

    def test_invalid_restore_source_does_not_touch_current(self):
        m.migrate(self.root)
        bad = self.root/'invalid.sqlite3';bad.write_bytes(b'not a database')
        with self.assertRaises(m.MigrationError):m.restore(self.root,bad)
        self.assertEqual(self.before,snapshot(self.path))

    def test_restore_copy_failure_recovers_current_version(self):
        result = m.migrate(self.root)
        actual_copy = m._copy_database
        calls = 0
        def fail_after_restore(source,target):
            nonlocal calls
            calls += 1
            actual_copy(source,target)
            if calls == 2:raise OSError('restore verification failure')
        with patch.object(m,'_copy_database',side_effect=fail_after_restore):
            with self.assertRaises(m.MigrationError) as caught:m.restore(self.root,result['backup'])
        self.assertTrue(caught.exception.recovery)
        self.assertTrue(Path(caught.exception.backup).is_file())
        with closing(m.read_only(self.path)) as db:self.assertEqual(m.LATEST_VERSION,m.version(db))
        self.assertEqual(self.before,snapshot(self.path))

    def test_recovery_failure_keeps_both_errors_and_backup(self):
        result=m.migrate(self.root)
        actual_copy=m._copy_database
        calls=0
        def fail_twice(source,target):
            nonlocal calls
            calls+=1
            if calls>1:raise OSError(f'failure-{calls}')
            actual_copy(source,target)
        with patch.object(m,'_copy_database',side_effect=fail_twice):
            with self.assertRaises(m.MigrationError) as caught:m.restore(self.root,result['backup'])
        exc=caught.exception
        self.assertFalse(exc.recovery)
        self.assertIn('failure-2',str(exc.cause));self.assertIn('failure-3',str(exc.recovery_error))
        self.assertTrue(Path(exc.backup).is_file());self.assertTrue(Path(result['backup']).is_file())

    def test_server_lifetime_lock_blocks_restore(self):
        result=m.migrate(self.root)
        with m.file_lock(self.root,'.runtime.lock'):
            with self.assertRaises(m.MigrationError) as caught:m.restore(self.root,result['backup'])
        self.assertEqual('lock',caught.exception.stage)

    def test_db_lock_blocks_startup_without_data_change(self):
        with m.file_lock(self.root,'.db-maintenance.lock'):
            with self.assertRaises(m.MigrationError):m.migrate(self.root)
        self.assertEqual(self.before,snapshot(self.path))

    def test_future_sequential_migration_records_history(self):
        step = m.Migration(m.LATEST_VERSION+1,'extra_lookup',('CREATE INDEX idx_revisions_user ON revisions(username)',))
        steps = m.MIGRATIONS+(step,)
        m.migrate(self.root,steps)
        self.assertEqual(self.before,snapshot(self.path))
        with closing(m.read_only(self.path)) as db:
            m.validate_schema(db,m.LATEST_VERSION+1,steps)
            self.assertEqual(list(range(1,m.LATEST_VERSION+2)),[x[0] for x in db.execute('SELECT version FROM schema_migrations ORDER BY version')])

    def test_process_crash_before_commit_preserves_legacy_and_retry_succeeds(self):
        # Only this disposable child deliberately exits; no user process is terminated.
        code = '''
import os,sys
import migrations as m
original=m._execute_step
def crash(db,step):
    original(db,step)
    db.execute("UPDATE documents SET name='uncommitted'")
    os._exit(23)
m._execute_step=crash
m.migrate(sys.argv[1])
'''
        child = subprocess.run([sys.executable,'-B','-c',code,str(self.root)],
                               cwd=Path(m.__file__).parent,capture_output=True,timeout=15)
        self.assertEqual(23,child.returncode,child.stderr.decode(errors='replace'))
        self.assertEqual(self.before,snapshot(self.path))
        backups = list((self.root/'backups').glob('*.sqlite3'))
        self.assertEqual(1,len(backups))
        self.assertEqual(self.before,snapshot(backups[0]))
        Store(self.root)
        self.assertEqual(self.before,snapshot(self.path))


if __name__ == '__main__':unittest.main()
