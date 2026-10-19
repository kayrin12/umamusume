"""Local-only portable panel; runs with the official Windows NuGet runtime."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import webbrowser
import urllib.request

from . import __version__
from .capture import CaptureError, windows
from .session import ROOT, Session


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, session):
        self.session = session
        self.token = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_hex(16)
        super().__init__(address, Handler)

    @property
    def origin(self):
        return f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value, content_type='application/json; charset=utf-8'):
        data = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Umacoach-Service', 'umacoach')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
                         f"script-src 'nonce-{self.server.token}'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler normally returns an HTML error document.
        self.reply(code, {'error': '本机助手无法处理该请求（HTTP '+str(code)+'）。'})

    def authorized(self, token_required=True):
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            self.reply(403, {'error': '不允许的访问地址'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            self.reply(403, {'error': '不允许的请求来源'})
            return False
        if token_required and not secrets.compare_digest(self.headers.get('X-Umacoach-Token', ''), self.server.token):
            self.reply(403, {'error': '请重新打开本机助手页面'})
            return False
        return True

    def do_GET(self):
        if not self.authorized(token_required=self.path != '/'):
            return
        if self.path == '/':
            html = ((ROOT/'umacoach/panel.html').read_text('utf-8')
                    .replace('__TOKEN__', self.server.token)
                    .replace('__LOCAL_ORIGIN__', self.server.origin)
                    .replace('__INSTANCE_ID__', self.server.instance_id))
            self.reply(200, html, 'text/html; charset=utf-8')
        elif self.path == '/api/health':
            self.reply(200, dict(service='umacoach', version=__version__, instance_id=self.server.instance_id))
        elif self.path == '/api/state':
            self.reply(200, self.server.session.snapshot())
        elif self.path == '/api/windows':
            try:
                self.reply(200, [{'hwnd': h, 'title': title} for h, title in windows()])
            except CaptureError as error:
                self.reply(503, {'error': str(error)})
            except Exception as error:
                self.reply(503, {'error': '读取窗口列表失败（'+type(error).__name__+'），请用前台选择或手动框选。'})
        elif self.path == '/api/diagnostics':
            self.reply(200, self.server.session.diagnostics())
        else:
            self.reply(404, {'error': '页面不存在'})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != '/api/action':
            self.reply(404, {'error': '操作不存在'})
            return
        try:
            if self.headers.get('Content-Type') != 'application/json':
                raise ValueError('请求需要JSON')
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 12_500_000:
                raise ValueError('请求过大或为空')
            data = json.loads(self.rfile.read(size))
            action, session = data['action'], self.server.session
            if action == 'configure':
                session.configure(data)
            elif action == 'read':
                session.read_now()
            elif action == 'pick':
                session.pick_window()
            elif action == 'region_prepare':
                self.reply(200, session.prepare_region())
                return
            elif action == 'region_select':
                session.select_region(data['selection_id'], data['rect'])
            elif action in ('new', 'resume', 'abandon'):
                session.boundary(action)
            elif action == 'auto':
                if type(data.get('enabled')) is not bool:
                    raise ValueError('缺少读取状态')
                session.toggle(data['enabled'])
            elif action == 'import':
                session.import_image(data['image'])
            elif action == 'exit':
                session.toggle(False)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                raise ValueError('未知操作')
            self.reply(200, {'ok': True})
        except CaptureError as error:
            self.reply(400, {'error': str(error)})
        except (ValueError, KeyError, TypeError):
            self.reply(400, {'error': '操作未完成，请检查窗口选择、配置或图片；如果正在分析，请稍候。'})
        except Exception:
            self.reply(500, {'error': '无法完成操作；请恢复游戏窗口并重试。'})


def check_local_server(server):
    """Probe our own server through a direct loopback connection, without proxies."""
    request = urllib.request.Request(server.origin+'/api/health', headers={'X-Umacoach-Token': server.token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        if response.headers.get('X-Umacoach-Service') != 'umacoach':
            raise RuntimeError('本机响应未通过助手身份校验')
        health = json.load(response)
    if health.get('service') != 'umacoach' or health.get('instance_id') != server.instance_id:
        raise RuntimeError('本机响应来自其他程序')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if sys.platform == 'win32':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    session = Session(Path(os.environ.get('UMACOACH_DATA_DIR', ROOT/'local-data')))
    stop = threading.Event()
    def sample_loop():
        while not stop.wait(1):
            session.tick()
    threading.Thread(target=sample_loop, daemon=True).start()
    with Server(('127.0.0.1', 0), session) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .25}, daemon=True)
        thread.start()
        try:
            check_local_server(server)
            print('赛马娘助手已启动并通过本机连接检查。请保留此窗口。', flush=True)
            print('本机面板地址：'+server.origin, flush=True)
            print('若浏览器没有自动打开，请复制上面地址打开；不要直接打开 panel.html。', flush=True)
            print('未配置视觉 API 时只预览，不上传画面。', flush=True)
            if not args.no_browser:
                webbrowser.open(server.origin)
            while thread.is_alive():
                thread.join(.5)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            session.toggle(False)
            server.shutdown()
            thread.join(2)


if __name__ == '__main__':
    main()
