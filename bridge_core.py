"""Local input queue for a console program. Python 3.9+, standard library only."""
import hmac
import json
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent


class JobStore:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs "
                            "(id TEXT PRIMARY KEY, text TEXT, status TEXT, result TEXT, created REAL)")
            if 'silent' not in {row['name'] for row in self.db.execute('PRAGMA table_info(jobs)')}:
                self.db.execute('ALTER TABLE jobs ADD COLUMN silent INTEGER NOT NULL DEFAULT 0')
            self.db.execute("UPDATE jobs SET status='failed', result=? "
                            "WHERE status IN ('queued','running')",
                            ("上次程序已停止；此消息未自动重试，请重新发送。",))

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def submit(self, job_id, text):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", job_id):
            raise ValueError("消息编号格式错误")
        if not isinstance(text, str) or len(text) > 16000:
            raise ValueError("消息须为不超过 16000 字的文字")
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise ValueError("消息包含不支持的控制字符")
        with self.lock, self.db:
            old = self.get(job_id)
            if old:
                if old['text'] != text:
                    raise ValueError("同一消息编号不能用于不同内容")
                return old
            count = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count >= 20:
                raise ValueError("等待队列已满，请稍后重新发送")
            self.db.execute("INSERT INTO jobs (id,text,status,result,created) VALUES (?,?,'queued','',?)", (job_id, text, time.time()))
            return self.get(job_id)

    def claim(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created, rowid LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (row['id'],))
            return row['id'], row['text']

    def finish(self, job_id, result, error=False, silent=False):
        if job_id is None:
            return
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status=?, result=?, silent=? WHERE id=? AND status='running'",
                            ('failed' if error else 'done', str(result), bool(silent) and not error, job_id))


class ChatIO:
    def __init__(self, directory=None, port=18766, console=True):
        self.directory = Path(directory or ROOT / '.runtime')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.console = console
        self.local_inputs = queue.Queue()
        self.reader_started = False
        self.ready = False
        self.stop_event = threading.Event()
        self.token = secrets.token_urlsafe(32)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def answer(self, code, value):
                body = json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                host = self.headers.get('Host', '')
                expected_hosts = {'127.0.0.1:%d' % bridge.port, 'localhost:%d' % bridge.port}
                supplied = self.headers.get('Authorization', '')
                expected = 'Bearer ' + bridge.token
                if host not in expected_hosts or self.headers.get('Origin') is not None:
                    self.answer(403, {'error': '仅允许本机桥接客户端'})
                    return False
                if not hmac.compare_digest(supplied, expected):
                    self.answer(401, {'error': '本机连接令牌不匹配，请重启 QQ 接入程序'})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path == '/health':
                    self.answer(200, {'ready': bridge.ready})
                elif self.path.startswith('/jobs/'):
                    item = bridge.store.get(self.path[len('/jobs/'):])
                    self.answer(200 if item else 404, item or {'error': '找不到消息'})
                else:
                    self.answer(404, {'error': '接口不存在'})

            def do_POST(self):
                if not self.authorized():
                    return
                if self.path != '/jobs':
                    self.answer(404, {'error': '接口不存在'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 100000:
                        raise ValueError('请求长度不正确')
                    if not self.headers.get('Content-Type', '').startswith('application/json'):
                        raise ValueError('请求须为 JSON')
                    value = json.loads(self.rfile.read(length).decode('utf-8'))
                    if not isinstance(value, dict):
                        raise ValueError('请求须为对象')
                    self.answer(200, bridge.store.submit(value.get('id'), value.get('text')))
                except (ValueError, UnicodeError) as error:
                    self.answer(400, {'error': str(error)})

        # Bind before recovering the database: another running instance must not be disturbed.
        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.port = self.server.server_address[1]
        self.store = JobStore(self.directory / 'jobs.sqlite3')
        self.connection_path = self.directory / 'connection.json'
        temp = self.directory / ('connection-' + secrets.token_hex(6) + '.tmp')
        with temp.open('w', encoding='utf-8') as file:
            json.dump({'url': 'http://127.0.0.1:%d' % self.port, 'token': self.token}, file)
        os.replace(str(temp), str(self.connection_path))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _read_console(self):
        while not self.stop_event.is_set():
            try:
                self.local_inputs.put(input())
            except (EOFError, KeyboardInterrupt):
                return

    def receive(self):
        self.ready = True
        if self.console:
            print('\n你：', end='', flush=True)
        if self.console and not self.reader_started:
            self.reader_started = True
            threading.Thread(target=self._read_console, daemon=True).start()
        while not self.stop_event.is_set():
            try:
                return None, self.local_inputs.get_nowait().strip()
            except queue.Empty:
                pass
            job = self.store.claim()
            if job:
                if self.console:
                    print('\n[QQ] ' + job[1], flush=True)
                return job[0], job[1]
            time.sleep(0.1)
        raise EOFError('QQ 桥接已停止')

    def finish(self, job_id, result, error=False, silent=False):
        if not self.stop_event.is_set():
            self.store.finish(job_id, result, error, silent)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.ready = False
        self.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        with self.store.lock:
            self.store.db.close()


class BridgeClient:
    def __init__(self, connection_path=None):
        self.connection_path = Path(connection_path or ROOT / '.runtime' / 'connection.json')
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, data=None):
        config = json.loads(self.connection_path.read_text(encoding='utf-8'))
        # The connection file only points at this computer, never at an external endpoint.
        if not re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}', config['url']):
            raise ValueError('本机连接地址不正确')
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
        request = Request(config['url'] + path, data=body, headers={
            'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=5) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            detail = json.loads(error.read().decode('utf-8')).get('error', str(error.code))
            raise ValueError(detail) from error
