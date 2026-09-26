#!/usr/bin/env python3
"""Install Todo Basic authentication. Run with the project's Python and sudo.

--check performs preflight and isolated tests without changing deployment files.
Credentials are generated into /etc/myapi/auth.env, never printed.
"""
import argparse
import asyncio
import base64
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

MAIN = Path('/opt/focus/main.py')
NGINX = Path('/etc/nginx/sites-available/default')
ENV = Path('/etc/myapi/auth.env')
DROPIN = Path('/etc/systemd/system/myapi.service.d/auth.conf')
DB = MAIN.parent / 'tasks.db'
EXPECTED = {
    MAIN: 'c4674cfe25b531bccffd3ae30553560dbf9175dc072e73cd61e59de0374847f1',
    NGINX: '165671acee54309bd921804c4165a4e32ddb887951cabb2dbd6193f146a841b1',
}
AUTH = '''import base64
import binascii
import os
import secrets
from fastapi import Request
from fastapi.responses import JSONResponse, Response

AUTH_USERNAME = os.environ.get("TODO_AUTH_USERNAME", "")
AUTH_PASSWORD = os.environ.get("TODO_AUTH_PASSWORD", "")
if not AUTH_USERNAME or not AUTH_PASSWORD or ":" in AUTH_USERNAME:
    raise RuntimeError("Set a nonempty TODO_AUTH_USERNAME and TODO_AUTH_PASSWORD; username cannot contain a colon")

app = FastAPI()


@app.middleware("http")
async def require_todo_auth(request: Request, call_next):
    authorization = request.headers.get("authorization", "")
    scheme, separator, encoded = authorization.partition(" ")
    valid = False
    if separator and scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(encoded, validate=True)
            username, colon, password = decoded.partition(b":")
            username_matches = secrets.compare_digest(username, AUTH_USERNAME.encode("utf-8"))
            password_matches = secrets.compare_digest(password, AUTH_PASSWORD.encode("utf-8"))
            valid = bool(colon) and username_matches and password_matches
        except (binascii.Error, ValueError):
            pass
    if not valid:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={
                "WWW-Authenticate": 'Basic realm="Todo", charset="UTF-8"',
                "Cache-Control": "no-store",
            },
        )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/__todo_auth", include_in_schema=False)
def check_todo_auth():
    return Response(status_code=204)
'''
NGINX_AUTH = '''
    # Authenticate every frontend asset and API request through the Todo app.
    auth_request /_todo_auth;
    add_header Cache-Control "no-store" always;

    location = /_todo_auth {
        internal;
        auth_request off;
        proxy_pass http://127.0.0.1:8000/__todo_auth;
        proxy_method GET;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Authorization $http_authorization;
    }
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def proposed_files():
    for path, expected in EXPECTED.items():
        if digest(path) != expected:
            raise RuntimeError(f'{path} changed since inspection; refusing to overwrite it')
    if ENV.exists() or DROPIN.exists():
        raise RuntimeError('Authentication config already exists; refusing to overwrite it')
    source = MAIN.read_text()
    nginx = NGINX.read_text()
    assert source.count('app = FastAPI()') == 1
    assert nginx.count('\tserver_name _;') == 1
    return source.replace('app = FastAPI()', AUTH), nginx.replace(
        '\tserver_name _;', '\tserver_name _;\n' + NGINX_AUTH, 1)


async def request(app, method, path, header=None, body=b''):
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {'type': 'http.request', 'body': body, 'more_body': False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    headers = [(b'content-type', b'application/json')]
    if header is not None:
        headers.append((b'authorization', header.encode('ascii')))
    await app({
        'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
        'http_version': '1.1', 'method': method, 'scheme': 'http',
        'path': path, 'raw_path': path.encode(), 'query_string': b'',
        'root_path': '', 'headers': headers,
        'client': ('127.0.0.1', 1234), 'server': ('test', 80),
    }, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    return start['status'], dict(start['headers']), b''.join(
        m.get('body', b'') for m in messages if m['type'] == 'http.response.body')


def isolated_tests(source):
    import json
    saved = {key: os.environ.get(key) for key in ('TODO_AUTH_USERNAME', 'TODO_AUTH_PASSWORD')}
    username, password = 'test-user', secrets.token_urlsafe(24)
    os.environ['TODO_AUTH_USERNAME'] = username
    os.environ['TODO_AUTH_PASSWORD'] = password
    try:
        namespace = {'__name__': 'todo_auth_test'}
        exec(compile(source, str(MAIN), 'exec'), namespace)
        with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as conn:
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0]
        with tempfile.TemporaryDirectory(prefix='todo-auth-test-') as directory:
            test_db = str(Path(directory) / 'tasks.db')
            with sqlite3.connect(test_db) as conn:
                conn.execute(schema)
            namespace['DB_FILE'] = test_db
            app = namespace['app']
            good = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
            wrong = 'Basic ' + base64.b64encode(b'wrong:wrong').decode()

            async def checks():
                endpoints = [('GET', '/tasks'), ('POST', '/tasks'),
                             ('PUT', '/tasks/1'), ('DELETE', '/tasks/1'),
                             ('GET', '/tasks/today'), ('GET', '/tasks/completed'),
                             ('GET', '/recommendation'), ('GET', '/docs'),
                             ('GET', '/redoc'), ('GET', '/openapi.json'),
                             ('GET', '/__todo_auth')]
                for method, path in endpoints:
                    for header in (None, wrong, 'Basic !!!', 'Bearer invalid'):
                        status, headers, _ = await request(app, method, path, header)
                        assert status == 401, (method, path, status)
                        assert headers[b'www-authenticate'].startswith(b'Basic ')
                for method, path in endpoints:
                    if method == 'GET':
                        status, headers, _ = await request(app, method, path, good)
                        assert status == (204 if path == '/__todo_auth' else 200), (path, status)
                        assert headers[b'cache-control'] == b'no-store'
                status, _, body = await request(app, 'POST', '/tasks', good,
                                                b'{"title":"Auth test","priority":"High"}')
                assert status == 200
                task_id = json.loads(body)['id']
                path = f'/tasks/{task_id}'
                assert (await request(app, 'PUT', path, good, b'{"status":"Done"}'))[0] == 200
                assert (await request(app, 'DELETE', path, good))[0] == 200
                assert (await request(app, 'PUT', path, good, b'{}'))[0] == 404
                assert (await request(app, 'DELETE', path, good))[0] == 404
            asyncio.run(checks())
        os.environ.pop('TODO_AUTH_PASSWORD')
        try:
            exec(compile(source, str(MAIN), 'exec'), {'__name__': 'missing_auth_test'})
        except RuntimeError:
            pass
        else:
            raise AssertionError('Missing credentials must prevent startup')
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print('PASS: isolated authentication, all existing API methods, docs, CRUD, missing-credential startup')


def http_check(url, method, authorization, expected):
    headers = {'Authorization': authorization} if authorization else {}
    req = urllib.request.Request(url, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        assert response.code == expected, (method, url, response.code, expected)
        if expected == 401:
            assert response.headers.get('WWW-Authenticate', '').startswith('Basic ')
        response.read()


def live_tests(username, password):
    good = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
    wrong = 'Basic ' + base64.b64encode(b'invalid:invalid').decode()
    for base, prefix in [('http://127.0.0.1:8000', ''), ('http://127.0.0.1', '/api')]:
        for path in ['/tasks', '/tasks/today', '/tasks/completed', '/recommendation',
                     '/recommendation?exclude_id=1', '/docs', '/redoc', '/openapi.json']:
            for auth in (None, wrong):
                http_check(base + prefix + path, 'GET', auth, 401)
            http_check(base + prefix + path, 'GET', good, 200)
        for method, path in [('POST', '/tasks'), ('PUT', '/tasks/1'), ('DELETE', '/tasks/1')]:
            for auth in (None, wrong):
                http_check(base + prefix + path, method, auth, 401)
    for path in ['/', '/index.html', '/index.html.backup', '/index.html.before_v1']:
        for auth in (None, wrong):
            http_check('http://127.0.0.1' + path, 'GET', auth, 401)
        http_check('http://127.0.0.1' + path, 'GET', good, 200)
    print('PASS: live frontend, frontend snapshots, API, docs, and denied write requests')


def install(source, nginx):
    if os.geteuid() != 0:
        raise SystemExit('Run installation with sudo; --check does not need sudo.')
    run('/usr/sbin/nginx', '-t')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path('/var/backups/myapi-auth') / stamp
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    backup.parent.chmod(0o700)
    for path in (MAIN, NGINX):
        shutil.copy2(path, backup / path.name)
    run('cp', '-a', str(DB), str(backup / DB.name))
    original_db_hash = digest(DB)
    frontend = Path('/var/www/html/index.html')
    original_frontend_hash = digest(frontend)
    username, password = 'todoadmin', secrets.token_urlsafe(32)
    created = []
    changed = []
    try:
        ENV.parent.mkdir(mode=0o700, exist_ok=True)
        DROPIN.parent.mkdir(mode=0o755, exist_ok=True)
        with ENV.open('x', opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            handle.write(f'TODO_AUTH_USERNAME={username}\nTODO_AUTH_PASSWORD={password}\n')
        created.append(ENV)
        with DROPIN.open('x') as handle:
            handle.write('[Service]\nEnvironmentFile=/etc/myapi/auth.env\n')
        created.append(DROPIN)
        changed.append(MAIN)
        MAIN.write_text(source)
        changed.append(NGINX)
        NGINX.write_text(nginx)
        run('/usr/sbin/nginx', '-t')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', 'myapi.service')
        for _ in range(40):
            try:
                http_check('http://127.0.0.1:8000/__todo_auth', 'GET', None, 401)
                break
            except (OSError, AssertionError):
                time.sleep(0.25)
        else:
            raise RuntimeError('Todo app did not become ready')
        run('systemctl', 'reload', 'nginx.service')
        time.sleep(1)
        live_tests(username, password)
        assert digest(DB) == original_db_hash, 'Task database changed during installation'
        assert digest(frontend) == original_frontend_hash, 'Frontend file unexpectedly changed'
        run('systemctl', 'is-active', 'myapi.service', 'nginx.service')
    except Exception:
        print(f'Installation failed; restoring deployment files from {backup}')
        for path in reversed(changed):
            shutil.copy2(backup / path.name, path)
        for path in created:
            path.unlink()
        run('systemctl', 'daemon-reload')
        run('/usr/sbin/nginx', '-t')
        run('systemctl', 'restart', 'myapi.service')
        run('systemctl', 'reload', 'nginx.service')
        raise
    print(f'Installed. Backups: {backup}')
    print('Changed: /opt/focus/main.py; /etc/nginx/sites-available/default')
    print('Created: /etc/myapi/auth.env (0600); /etc/systemd/system/myapi.service.d/auth.conf')
    print('Database and frontend file hashes unchanged. Xray and port 443 were not modified.')
    print('View login credentials privately in your terminal: sudo cat /etc/myapi/auth.env')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    source, nginx = proposed_files()
    isolated_tests(source)
    if args.check:
        print('Preflight passed. No deployment files changed.')
    else:
        install(source, nginx)


if __name__ == '__main__':
    main()
