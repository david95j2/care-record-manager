"""Local development persistence; files and DB stay on the API host."""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from domain import ValidationError, compare, validate_draft, timestamp, minutes
from migrations import migrate, file_lock, version, LATEST_VERSION, MigrationError


class Conflict(Exception):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.migration_result = migrate(self.root)

    @contextmanager
    def connect(self):
        with file_lock(self.root, '.db-maintenance.lock'):
            db = sqlite3.connect(self.root / 'development.sqlite3', timeout=10)
            try:
                if version(db) != LATEST_VERSION:
                    raise MigrationError('runtime-version', 'DB 버전이 변경되었습니다. 서버를 다시 시작하세요.')
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                with db:
                    yield db
            finally:
                db.close()

    def add_user(self, name, password):
        if not isinstance(name, str) or not isinstance(password, str) or not name.strip() or len(name) > 120 or len(password) < 12 or len(password) > 1024:
            raise ValidationError('계정명과 12자 이상의 비밀번호가 필요합니다.')
        name = name.strip()
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000)
        with self.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE name=?', (name,)).fetchone():
                raise Conflict('이미 존재하는 계정명입니다.')
            db.execute('INSERT INTO users VALUES(?,?,?)', (name, salt, digest))

    def manage_users(self, actor, create=None):
        if actor != 'admin':
            raise PermissionError('admin 계정만 사용자 관리에 접근할 수 있습니다.')
        if create is not None:
            self.add_user(create.get('username'), create.get('password'))
        with self.connect() as db:
            return [{'username': r[0], 'administrator': r[0] == 'admin'}
                    for r in db.execute('SELECT name FROM users ORDER BY name')]

    def login(self, name, password):
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE name=?', (name,)).fetchone()
            salt = row['salt'] if row else b'no-such-account!'
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000)
            if not row or not secrets.compare_digest(digest, row['digest']):
                return None
            token = secrets.token_urlsafe(32)
            db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), name, time.time()+28800))
            return token

    def user(self, token):
        with self.connect() as db:
            row = db.execute('SELECT username FROM sessions WHERE token=? AND expires>?',
                             (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            return row['username'] if row else None

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def upload(self, name, data, user):
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValidationError('파일 크기는 20MB 이하여야 합니다.')
        mime = ('application/pdf' if data.startswith(b'%PDF-') else 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n')
                else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else None)
        if not mime:
            raise ValidationError('PDF, PNG, JPEG 파일만 지원합니다.')
        ident = str(uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO documents(id,name,mime,original,sha256,created) VALUES(?,?,?,?,?,?)',
                       (ident, name[:240], mime, data, hashlib.sha256(data).hexdigest(), time.time()))
            self.audit(db, user, 'upload', ident, 1)
        return ident

    def list(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute('SELECT id,name,version,created FROM documents ORDER BY created DESC')]

    def get(self, ident, original=False):
        with self.connect() as db:
            cols = 'original,mime' if original else 'id,name,version,draft,sha256'
            row = db.execute(f'SELECT {cols} FROM documents WHERE id=?', (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            result = dict(row)
            if not original:
                result['draft'] = json.loads(result['draft'])
                result['results'] = [compare(x) for x in result['draft']['sales']]
                result['confirmedVersions'] = [x[0] for x in db.execute('SELECT version FROM revisions WHERE document_id=? ORDER BY version', (ident,))]
            return result

    @staticmethod
    def audit(db, user, action, ident, version):
        db.execute('INSERT INTO audit(username,action,document_id,version,created) VALUES(?,?,?,?,?)',
                   (user, action, ident, version, time.time()))

    def save(self, ident, version, draft, user):
        validate_draft(draft)
        with self.connect() as db:
            cursor = db.execute('UPDATE documents SET draft=?,version=version+1 WHERE id=? AND version=?',
                                (json.dumps(draft, ensure_ascii=False), ident, version))
            if cursor.rowcount != 1:
                raise Conflict('다른 화면에서 수정되었습니다. 다시 불러와 확인하세요.')
            self.audit(db, user, 'save_draft', ident, version+1)
        return self.get(ident)

    def confirm(self, ident, version, user):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM documents WHERE id=?', (ident,)).fetchone()
            if not row or row['version'] != version:
                raise Conflict('다른 화면에서 수정되었습니다. 다시 불러와 확인하세요.')
            draft = validate_draft(json.loads(row['draft']))
            if not draft['sales']:
                raise ValidationError('확정할 매출 입력이 없습니다.')
            results = [compare(x) for x in draft['sales']]
            for sale in draft['sales']:
                if sale.get('decision') not in ('accepted', 'rejected', 'not_provided'):
                    raise ValidationError('각 매출에 사용자 최종 판정을 선택하세요. 보류는 확정할 수 없습니다.')
                if sale.get('returned') is True and sale['decision'] != 'not_provided':
                    raise ValidationError('반납 거래는 서비스 미실시로 확정하세요.')
                if sale.get('returned') is not True:
                    if compare(sale)['actualMinutes'] is None:
                        raise ValidationError('계획·실제 서비스 구간을 먼저 확인하세요. 미확정 값은 보류로 남기세요.')
                    record = sale['record']
                    timestamp(record.get('start'))
                    timestamp(record.get('end'))
                    minutes(record.get('minutes'))
                    if not str(sale.get('approval') or '').strip() or not str(record.get('approval') or '').strip():
                        raise ValidationError('매출과 기록지 승인번호가 필요합니다.')
            db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?)',
                       (str(uuid4()), ident, version, row['draft'], json.dumps(results, ensure_ascii=False), user, time.time()))
            self.audit(db, user, 'confirm', ident, version)
        return self.get(ident)
