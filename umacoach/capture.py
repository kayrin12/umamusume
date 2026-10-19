"""Visible Windows client-area capture. No game memory access or input hooks."""
import sys
import ctypes
from ctypes import wintypes
from functools import lru_cache
from types import SimpleNamespace
from io import BytesIO
import base64
from PIL import ImageChops, ImageStat


def distance(a, b):
    a = a.convert('RGB').resize((96, 96))
    b = b.convert('RGB').resize((96, 96))
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def changed(a, b):
    if distance(a, b) > 2.5:
        return True
    # Date/energy text may change while most of the scene remains identical.
    diff = ImageChops.difference(a.convert('RGB').resize((192, 192)),
                                b.convert('RGB').resize((192, 192)))
    return any(sum(ImageStat.Stat(diff.crop((x, y, x+24, y+24))).mean)/3 > 10
               for y in range(0, 192, 24) for x in range(0, 192, 24))


class Gate:
    """Require a stable changed frame and space out paid requests."""
    def __init__(self, interval=8):
        self.interval = interval
        self.previous = None
        self.sent = None
        self.sent_at = float('-inf')
        self.changed_at = None

    def ready(self, frame, now):
        stable = self.previous is not None and distance(frame, self.previous) < 1.5
        self.previous = frame.copy()
        is_changed = self.sent is None or changed(frame, self.sent)
        if not is_changed:
            self.changed_at = None
            return False
        if self.changed_at is None:
            self.changed_at = now
        # Character/background animation must not block analysis forever.
        settled = stable or now-self.changed_at >= 20
        return settled and now-self.sent_at >= self.interval

    def mark(self, frame, now):
        self.sent = frame.copy()
        self.sent_at = now
        self.changed_at = None


class CaptureError(RuntimeError):
    """Safe capture diagnostics without API keys or environment contents."""


@lru_cache(maxsize=1)
def _api():
    if sys.platform != 'win32':
        raise CaptureError('本机运行系统不是 Windows，无法读取 Windows 游戏窗口。')
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    specs = {
        'EnumWindows': ([callback, wintypes.LPARAM], wintypes.BOOL),
        'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
        'IsWindow': ([wintypes.HWND], wintypes.BOOL),
        'IsIconic': ([wintypes.HWND], wintypes.BOOL),
        'GetWindowTextLengthW': ([wintypes.HWND], ctypes.c_int),
        'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        'GetClientRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        'ClientToScreen': ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        'GetForegroundWindow': ([], wintypes.HWND),
        'GetAncestor': ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        'GetSystemMetrics': ([ctypes.c_int], ctypes.c_int),
    }
    for name, (args, result) in specs.items():
        function = getattr(user, name)
        function.argtypes, function.restype = args, result
    return SimpleNamespace(user=user, callback=callback)


def window_title(hwnd, user=None):
    user = user or _api().user
    length = min(max(user.GetWindowTextLengthW(hwnd), 0), 2048)
    title = ctypes.create_unicode_buffer(length+1)
    user.GetWindowTextW(hwnd, title, len(title))
    if title.value.strip():
        return title.value.strip()
    kind = ctypes.create_unicode_buffer(256)
    user.GetClassNameW(hwnd, kind, len(kind))
    return '无标题窗口 · '+(kind.value or '未知类型')


def windows():
    if sys.platform != 'win32':
        return []
    api = _api()
    user = api.user
    result = []
    def collect(hwnd, _):
        if user.IsWindowVisible(hwnd):
            result.append((int(hwnd), window_title(hwnd, user)))
        return True
    if not user.EnumWindows(api.callback(collect), 0):
        raise CaptureError('Windows 未能枚举窗口，请使用“3秒后选择前台窗口”或“手动框选”。')
    hints = ('赛马娘', '賽馬娘', 'ウマ娘', 'umamusume')
    result.sort(key=lambda item: not any(word in item[1].lower() for word in hints))
    return result


def foreground_window():
    user = _api().user
    hwnd = user.GetForegroundWindow()
    if not hwnd:
        raise CaptureError('没有取得前台窗口，请重新点击选择按钮，然后切换到游戏。')
    hwnd = user.GetAncestor(hwnd, 2) or hwnd
    title = window_title(hwnd, user)
    if '养成助手' in title:
        raise CaptureError('选到了助手页面，请重试，并在3秒内切换到游戏窗口。')
    return int(hwnd), title


def capture(hwnd):
    from PIL import ImageGrab
    user = _api().user
    if not user.IsWindow(hwnd) or user.IsIconic(hwnd):
        raise CaptureError('游戏窗口已关闭或最小化，请恢复窗口后重新选择。')
    rect, point = wintypes.RECT(), wintypes.POINT(0, 0)
    if not user.GetClientRect(hwnd, ctypes.byref(rect)) or not user.ClientToScreen(hwnd, ctypes.byref(point)):
        raise CaptureError('无法获取游戏窗口区域，请改用“手动框选游戏画面”。')
    if rect.right < 50 or rect.bottom < 50:
        raise CaptureError('窗口区域过小，请恢复游戏窗口或改用“手动框选”。')
    try:
        return ImageGrab.grab(bbox=(point.x, point.y, point.x+rect.right, point.y+rect.bottom), all_screens=True)
    except OSError as error:
        raise CaptureError('屏幕截图失败，请使用窗口化/无边框模式并保持游戏可见。') from error


def desktop_bounds():
    user = _api().user
    left, top, width, height = [user.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
    if width <= 0 or height <= 0:
        raise CaptureError('无法取得屏幕尺寸，请在本地 Windows 桌面运行助手。')
    return left, top, width, height


def desktop_snapshot():
    """Local-only selection image; never passed to the vision analyzer."""
    from PIL import ImageGrab
    left, top, width, height = desktop_bounds()
    image = ImageGrab.grab(all_screens=True)
    if image.size != (width, height):
        raise CaptureError('屏幕缩放尺寸不一致，请重启助手后重新框选。')
    image.thumbnail((1600, 1000))
    buf = BytesIO()
    image.convert('RGB').save(buf, 'JPEG', quality=80)
    return dict(image=base64.b64encode(buf.getvalue()).decode(),
                preview_size=list(image.size), bounds=[left, top, width, height])


def selection_box(rect, preview_size, bounds):
    if not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int for v in rect):
        raise CaptureError('请选择有效画面区域。')
    x, y, w, h = rect
    pw, ph = preview_size
    left, top, width, height = bounds
    if min(x, y) < 0 or min(w, h) <= 0 or x+w > pw or y+h > ph:
        raise CaptureError('框选范围超出屏幕，请重新拖动选择。')
    x1, y1 = left+round(x*width/pw), top+round(y*height/ph)
    x2, y2 = left+round((x+w)*width/pw), top+round((y+h)*height/ph)
    if x2-x1 < 50 or y2-y1 < 50:
        raise CaptureError('框选范围太小，请包含完整的游戏画面。')
    return (x1, y1, x2, y2)


def capture_region(box):
    from PIL import ImageGrab
    left, top, width, height = desktop_bounds()
    x1, y1, x2, y2 = box
    if x1 < left or y1 < top or x2 > left+width or y2 > top+height:
        raise CaptureError('屏幕布局已变化，请重新框选游戏区域。')
    return ImageGrab.grab(bbox=box, all_screens=True)
