"""Loopback-only development server. Not a LAN production deployment."""
import argparse
import base64
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import time
from http.cookies import SimpleCookie
from pathlib import Path
from wsgiref.simple_server import make_server, WSGIRequestHandler
from domain import ValidationError
from store import Store, Conflict
from migrations import file_lock, restore, MigrationError
from sales_store import SalesStore
from urllib.parse import parse_qs

STATIC = Path(__file__).parent / 'static'


class RequestLogHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        logging.info(fmt, *args)


class App:
    def __init__(self, store, origin):
        self.store, self.origin = store, origin
        self.sales = SalesStore(store)
        self.attempts = []

    def __call__(self, env, start):
        headers = [('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                   ('Referrer-Policy', 'no-referrer'), ('X-Frame-Options', 'SAMEORIGIN'),
                   ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")]
        code = 200
        mime = 'application/json; charset=utf-8'
        try:
            path, method = env['PATH_INFO'], env['REQUEST_METHOD']
            if env.get('HTTP_HOST') != self.origin.split('://')[1]:
                raise PermissionError('허용되지 않은 서버 주소입니다.')
            if method not in ('GET', 'POST'):
                raise ValidationError('지원하지 않는 요청입니다.')
            if method == 'POST':
                if env.get('HTTP_ORIGIN') != self.origin:
                    raise PermissionError('다른 사이트의 요청은 허용하지 않습니다.')
                size = int(env.get('CONTENT_LENGTH', '0') or 0)
                if size > 28 * 1024 * 1024 or size <= 0:
                    raise ValidationError('요청 크기가 잘못되었습니다.')
                if env.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
                    raise ValidationError('JSON 요청만 지원합니다.')
                body = json.loads(env['wsgi.input'].read(size))
                if not isinstance(body, dict):
                    raise ValidationError('잘못된 요청입니다.')
            cookie = SimpleCookie(env.get('HTTP_COOKIE', ''))
            token = cookie['session'].value if 'session' in cookie else ''
            user = self.store.user(token)
            if path == '/api/login' and method == 'POST':
                self.attempts = [t for t in self.attempts if t > time.time()-60]
                if len(self.attempts) >= 10:
                    raise PermissionError('로그인 시도가 많습니다. 1분 뒤 다시 시도하세요.')
                self.attempts.append(time.time())
                token = self.store.login(str(body.get('username', ''))[:120], str(body.get('password', ''))[:1024])
                if not token:
                    raise PermissionError('계정 또는 비밀번호를 확인하세요.')
                headers.append(('Set-Cookie', f'session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'))
                value = {'ok': True}
            elif path in ('/', '/app.js', '/sales.js', '/autosave.js', '/theme.js', '/style.css') and method == 'GET':
                name = 'index.html' if path == '/' else path[1:]
                mime = 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/html; charset=utf-8' if name=='index.html' else 'text/css; charset=utf-8'
                value = (STATIC / name).read_bytes()
            elif path == '/api/health' and method == 'GET':
                value = {'status': 'development', 'ai': 'not_connected', 'lanEnabled': False}
            elif not user:
                raise PermissionError('로그인이 필요합니다.')
            elif path == '/api/logout' and method == 'POST':
                self.store.logout(token)
                headers.append(('Set-Cookie', 'session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'))
                value = {'ok': True}
            elif path == '/api/me' and method == 'GET':
                value = {'username': user, 'administrator': user == 'admin'}
            elif path == '/api/users':
                value = self.store.manage_users(user, body if method == 'POST' else None)
            elif path == '/api/sales-imports' and method == 'GET':
                value = self.sales.imports()
            elif path == '/api/sales-imports' and method == 'POST':
                value = self.sales.upload(body.get('name'),base64.b64decode(body.get('data',''),validate=True),user)
            elif path.startswith('/api/sales-imports/') and method == 'GET':
                parts = path.split('/')
                if len(parts) == 5 and parts[4] == 'original':
                    value = self.sales.original(parts[3])
                    mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                    headers.append(('Content-Disposition','attachment; filename="sales-original.xlsx"'))
                elif len(parts) == 4:
                    query = parse_qs(env.get('QUERY_STRING',''))
                    value = self.sales.rows(parts[3],int(query.get('page',['1'])[0]),
                                            query.get('q',[''])[0],query.get('category',['all'])[0])
                else:
                    raise KeyError(path)
            elif path.startswith('/api/sales-rows/') and len(path.split('/')) == 4:
                ident = path.split('/')[3]
                value = self.sales.get(ident) if method == 'GET' else self.sales.save(ident,body['version'],body['draft'],user)
            elif path == '/api/documents' and method == 'GET':
                value = self.store.list()
            elif path == '/api/documents' and method == 'POST':
                raw = base64.b64decode(body.get('data', ''), validate=True)
                value = {'id': self.store.upload(str(body.get('name', '문서')), raw, user)}
            elif path.startswith('/api/documents/'):
                parts = path.split('/')
                ident = parts[3]
                action = parts[4] if len(parts) == 5 else ''
                if len(parts) > 5:
                    raise KeyError(path)
                if method == 'GET' and not action:
                    value = self.store.get(ident)
                elif method == 'GET' and action == 'original':
                    source = self.store.get(ident, original=True)
                    value, mime = source['original'], source['mime']
                elif method == 'POST' and action == 'save':
                    value = self.store.save(ident, body['version'], body['draft'], user)
                elif method == 'POST' and action == 'confirm':
                    value = self.store.confirm(ident, body['version'], user)
                else:
                    raise KeyError(path)
            else:
                raise KeyError(path)
        except PermissionError as exc:
            code, value = 403, {'error': str(exc)}
        except Conflict as exc:
            code, value = 409, {'error': str(exc)}
        except sqlite3.IntegrityError:
            code, value = 409, {'error': '이미 확정된 버전입니다. 수정 시 새 버전으로 저장하세요.'}
        except KeyError:
            code, value = 404, {'error': '요청한 항목이 없거나 필수 입력이 빠졌습니다.'}
        except (ValidationError, ValueError, TypeError) as exc:
            code, value = 400, {'error': str(exc) if isinstance(exc, ValidationError) else '입력 형식을 확인하세요.'}
        except Exception:
            logging.exception('Request failed')
            code, value = 500, {'error': '저장하지 못했습니다. 서버 로그를 확인하세요.'}
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        headers.extend([('Content-Type', mime), ('Content-Length', str(len(data)))])
        labels = {200:'OK', 400:'Bad Request',403:'Forbidden',404:'Not Found',409:'Conflict',500:'Internal Server Error'}
        start(f'{code} {labels[code]}', headers)
        return [data]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['serve', 'create-user', 'migrate', 'restore'])
    parser.add_argument('--data', default=str(Path(__file__).parent / 'data'))
    parser.add_argument('--port', type=int, default=8877)
    parser.add_argument('--log-dir', help='백그라운드 실행 로그 폴더')
    parser.add_argument('--backup', help='복원할 SQLite 백업 파일')
    parser.add_argument('--confirm-restore', action='store_true', help='현재 DB를 백업 시점으로 되돌리는 것을 승인')
    args = parser.parse_args()
    if args.log_dir:
        log_dir = Path(args.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / 'server.log', maxBytes=2*1024*1024, backupCount=5, encoding='utf-8')
        logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'restore':
        if not args.backup or not args.confirm_restore:
            parser.error('복원은 --backup 경로와 --confirm-restore가 필요합니다. 서버를 먼저 종료하세요.')
        print(json.dumps(restore(args.data, args.backup), ensure_ascii=False))
        return
    with file_lock(args.data, '.runtime.lock'):
        run_command(args)


def run_command(args):
    store = Store(args.data)
    logging.info('DB ready: %s', store.migration_result)
    print(json.dumps({'migration': store.migration_result}, ensure_ascii=False), flush=True)
    if args.command == 'migrate':
        return
    if args.command == 'create-user':
        username = input('계정명: ').strip()
        password = getpass.getpass('비밀번호 (12자 이상): ')
        if password != getpass.getpass('비밀번호 확인: '):
            raise SystemExit('비밀번호가 일치하지 않습니다.')
        store.add_user(username, password)
        print('계정 생성 완료')
        return
    origin = f'http://127.0.0.1:{args.port}'
    print(f'{origin} — 개발용, AI 미연결, 내부망 배포 전용 서버 아님', flush=True)
    logging.info('Server starting: %s', origin)
    with make_server('127.0.0.1', args.port, App(store, origin), handler_class=RequestLogHandler) as httpd:
        httpd.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except MigrationError as exc:
        logging.error('%s', exc)
        raise SystemExit(1) from exc
