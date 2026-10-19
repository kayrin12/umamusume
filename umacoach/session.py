"""Shared local session for the portable browser panel, without Tcl/Tk."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import threading
import time
import secrets
import struct
import sys

from PIL import Image

from . import __version__

from .capture import (CaptureError, Gate, capture, capture_region, desktop_snapshot,
                      distance, foreground_window, selection_box, windows)
from .tracker import Tracker
from .vision import analyze


ROOT = Path(__file__).resolve().parent.parent


class Session:
    def __init__(self, data_dir: Path):
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self.tracker = Tracker(data_dir/'runs.json')
        self.key = os.environ.get('UMACOACH_API_KEY', '')
        self.endpoint = os.environ.get('UMACOACH_API_URL', 'https://api.openai.com/v1')
        self.model = os.environ.get('UMACOACH_MODEL', 'gpt-4.1-mini')
        self.hwnd = None
        self.target_label = ''
        self.region = None
        self.region_selection = None
        self.picker = None
        self.capture_error = ''
        self.auto = False
        self.busy = False
        self.epoch = 0
        self.gate = Gate()
        self.requested_at = float('-inf')
        self.frame = None
        self.preview = ''
        self.sample = False
        self.history = []
        self.result = None
        self.status = '选择游戏窗口，点击“应用配置”，然后开始读取。未配置 API 也能本地预览。'

    def snapshot(self):
        with self.lock:
            run = self.tracker.active
            return dict(status=self.status, auto=self.auto, busy=self.busy,
                        has_key=bool(self.key), endpoint=self.endpoint, model=self.model,
                        hwnd=self.hwnd, target_label=self.target_label, region=self.region,
                        picking=bool(self.picker), preview=self.preview, result=self.result,
                        pending=self.tracker.pending, sample=self.sample,
                        run_id=run.id if run else None,
                        character=run.character if run else '',
                        run_count=len(self.tracker.runs))

    def configure(self, data):
        with self.lock:
            hwnd = data.get('hwnd')
            if hwnd == 'region':
                if self.region is None:
                    raise CaptureError('请先框选游戏画面。')
                hwnd = None
                region = self.region
                label = self.target_label
            else:
                hwnd = int(hwnd) if hwnd else None
                region = None
                labels = ({self.hwnd: self.target_label} if hwnd == self.hwnd else dict(windows())) if hwnd is not None else {}
                if hwnd is not None and hwnd not in labels and hwnd != self.hwnd:
                    raise CaptureError('该窗口未在列表中，请使用前台窗口选择或手动框选。')
                label = labels.get(hwnd, self.target_label if hwnd == self.hwnd else '')
            endpoint = data.get('endpoint', self.endpoint).strip()
            model = data.get('model', self.model).strip()
            if not endpoint or not model:
                raise ValueError('请填写 API 地址和模型；尚无密钥也能使用本地预览')
            self.hwnd, self.endpoint, self.model = hwnd, endpoint, model
            self.region, self.target_label = region, label
            self.picker = None
            if data.get('key', '').strip():
                self.key = data['key'].strip()
            self.auto = False
            self.epoch += 1
            self.gate = Gate()
            self.frame, self.preview, self.sample = None, '', False
            self.history, self.result = [], None
            self.status = '配置已应用，密钥仅保留在本机进程内存。'

    def read_frame(self):
        return capture_region(self.region) if self.region is not None else capture(self.hwnd)

    def pick_window(self, delay=3):
        with self.lock:
            self.auto = False
            self.epoch += 1
            epoch = self.epoch
            token = self.picker = secrets.token_hex(12)
            self.result, self.history = None, []
            self.status = '请在3秒内点击或切换到游戏窗口，保持游戏在前台。'
        def choose():
            try:
                with self.lock:
                    if self.picker != token or self.epoch != epoch:
                        return
                hwnd, title = foreground_window()
                frame = capture(hwnd)
                with self.lock:
                    if self.picker != token or self.epoch != epoch:
                        return
                    self.epoch += 1
                    self.hwnd, self.region, self.target_label = hwnd, None, title
                    self.sample = False
                    self.gate = Gate()
                    self.set_frame(frame)
                    self.capture_error = ''
                    self.status = '已选择前台窗口并预览：'+title+'。点击“开始持续读取”即可。'
            except CaptureError as error:
                with self.lock:
                    if self.picker == token and self.epoch == epoch:
                        self.capture_error = str(error)
                        self.status = str(error)
            except Exception:
                with self.lock:
                    if self.picker == token and self.epoch == epoch:
                        self.capture_error = '前台窗口采集失败，请使用“手动框选游戏画面”。'
                        self.status = self.capture_error
            finally:
                with self.lock:
                    if self.picker == token:
                        self.picker = None
        timer = threading.Timer(delay, choose)
        timer.daemon = True
        timer.start()

    def prepare_region(self):
        with self.lock:
            self.auto = False
            self.epoch += 1
            self.picker = None
            self.result, self.history = None, []
            self.status = '桌面截图仅在本机用于框选；确认后只读取所选游戏区域。'
            snapshot = desktop_snapshot()
            token = secrets.token_hex(16)
            self.region_selection = dict(token=token, expires=time.monotonic()+300,
                                         preview_size=snapshot['preview_size'], bounds=snapshot['bounds'])
            return dict(image=snapshot['image'], preview_size=snapshot['preview_size'], selection_id=token)

    def select_region(self, token, rect):
        with self.lock:
            selection = self.region_selection
            if not selection or token != selection['token'] or time.monotonic() > selection['expires']:
                raise CaptureError('框选已过期，请重新点击“手动框选游戏画面”。')
            box = selection_box(rect, selection['preview_size'], selection['bounds'])
            frame = capture_region(box)
            self.auto = False
            self.epoch += 1
            self.hwnd, self.region = None, box
            self.target_label = '手动框选区域（游戏移动后需重新框选）'
            self.picker, self.region_selection = None, None
            self.result, self.history = None, []
            self.gate, self.sample = Gate(), False
            self.set_frame(frame)
            self.capture_error = ''
            self.status = '已选定游戏区域并预览。点击“开始持续读取”；游戏移动后需重新框选。'

    def diagnostics(self):
        with self.lock:
            data = dict(app_version=__version__, platform=sys.platform, python=sys.version.split()[0],
                        pointer_bits=struct.calcsize('P')*8, hwnd=self.hwnd,
                        capture_mode='region' if self.region is not None else 'window',
                        last_capture_error=self.capture_error)
        try:
            data['visible_window_count'] = len(windows())
        except CaptureError as error:
            data['enumeration_error'] = str(error)
        except Exception as error:
            data['enumeration_error'] = type(error).__name__
        return data

    def boundary(self, kind):
        with self.lock:
            self.epoch += 1
            self.gate = Gate()
            self.history, self.result = [], None
            if kind == 'new':
                self.tracker.new()
            elif kind == 'resume':
                if self.tracker.active:
                    self.tracker.active.last_turn = None
                    self.tracker.active.character = ''
                self.tracker.resume()
                self.tracker.save()
            elif kind == 'abandon':
                self.tracker.close('manual_abandon')
            else:
                raise ValueError('未知局次操作')
            self.status = '局次已更新，旧建议已清除。'

    def set_frame(self, frame):
        self.frame = frame.copy()
        thumb = frame.convert('RGB')
        thumb.thumbnail((420, 640))
        out = BytesIO()
        thumb.save(out, format='JPEG', quality=75)
        self.preview = base64.b64encode(out.getvalue()).decode()

    def import_image(self, data):
        if len(data) > 12_000_000:
            raise ValueError('图片过大，请使用小于8MB的截图')
        raw = base64.b64decode(data, validate=True)
        with Image.open(BytesIO(raw)) as image:
            if image.width*image.height > 24_000_000:
                raise ValueError('图片尺寸过大')
            frame = image.convert('RGB')
        with self.lock:
            if self.busy:
                raise ValueError('请等待当前分析完成再导入')
            self.epoch += 1
            self.auto, self.sample = False, True
            self.result = None
            self.set_frame(frame)
            self.status = '样本已载入；样本分析不会修改真实局次。'
            if self.key:
                self.submit(frame, True)

    def read_now(self):
        with self.lock:
            if self.busy:
                raise ValueError('上一张画面仍在分析，请稍候')
            if self.sample and self.frame is not None:
                frame = self.frame.copy()
            else:
                if self.hwnd is None and self.region is None:
                    raise ValueError('请先选择游戏窗口并应用配置，或导入截图')
                try:
                    frame = self.read_frame()
                    self.capture_error = ''
                except CaptureError as error:
                    self.capture_error = str(error)
                    self.status = str(error)
                    self.result = None
                    raise
                self.set_frame(frame)
            if not self.key:
                self.result = None
                self.status = '已读取画面，仅在本机预览。填写视觉 API 密钥后才能给出建议。'
                return
            self.submit(frame, self.sample)

    def toggle(self, enabled):
        with self.lock:
            if enabled and self.hwnd is None and self.region is None:
                raise ValueError('请先选择游戏窗口并应用配置')
            self.auto = enabled
            self.epoch += 1
            self.gate = Gate()
            self.result = None
            if enabled:
                self.sample = False
            self.status = ('正在读取可见游戏窗口。' if self.key else '正在本地预览；尚未配置 API，不会上传画面。') if enabled else '已暂停读取。'

    def tick(self):
        with self.lock:
            if not self.auto:
                return
            try:
                frame = self.read_frame()
                self.set_frame(frame)
                ready = self.gate.ready(frame, time.monotonic())
                if ready and not self.busy and self.key and time.monotonic()-self.requested_at >= 8:
                    self.submit(frame, False)
            except CaptureError as error:
                self.auto = False
                self.result = None
                self.capture_error = str(error)
                self.status = str(error)
            except Exception:
                self.auto = False
                self.result = None
                self.capture_error = '无法读取游戏窗口，请改用前台选择或手动框选。'
                self.status = self.capture_error

    def submit(self, frame, sample):
        # Caller holds the reentrant lock; only one worker may be in flight.
        if self.busy:
            raise ValueError('上一张画面仍在分析')
        self.busy = True
        self.requested_at = time.monotonic()
        epoch, endpoint, key, model = self.epoch, self.endpoint, self.key, self.model
        run = self.tracker.active
        context = {} if sample else dict(run_id=run.id if run else None,
                                        pending_boundary=self.tracker.pending,
                                        recent_observations=self.history[-4:])
        self.result = None
        self.status = '正在分析截图样本…' if sample else '正在分析当前游戏画面…'

        def work():
            try:
                result = analyze(frame, endpoint, key, model, context, ROOT/'guides/legendary_tw.md')
                with self.lock:
                    if epoch != self.epoch:
                        return
                    if not sample and self.auto and self.frame is not None and distance(self.frame, frame) > 12:
                        self.status = '分析期间画面已明显变化，过时结果已丢弃。'
                        return
                    self.gate.mark(frame, time.monotonic())
                    prior = self.tracker.active.id if self.tracker.active else None
                    self.status = '截图样本模式，不修改真实局次。' if sample else self.tracker.apply(result)
                    current = self.tracker.active.id if self.tracker.active else None
                    if not sample:
                        if current != prior or self.tracker.pending:
                            self.history = []
                        if not self.tracker.pending:
                            if self.tracker.active:
                                self.history = (self.history+[result])[-4:]
                            self.data_dir.mkdir(parents=True, exist_ok=True)
                            with (self.data_dir/'observations.jsonl').open('a', encoding='utf-8') as f:
                                f.write(json.dumps(dict(at=time.time(), run_id=current or prior, observation=result), ensure_ascii=False)+'\n')
                    # Uncertain boundaries and low confidence never expose an action.
                    if (sample or not self.tracker.pending) and result['confidence'] >= .85:
                        self.result = result
                    elif result['confidence'] < .85:
                        self.status = '画面识别不确定，暂不显示建议；请等画面稳定后重新读取。'
            except Exception:
                with self.lock:
                    if epoch == self.epoch:
                        self.auto = False
                        self.result = None
                        # Neither API errors nor logs expose entered keys or request data.
                        self.status = '分析失败，已暂停。请检查 API 地址、模型、密钥、配额和网络后重试。'
            finally:
                with self.lock:
                    self.busy = False

        threading.Thread(target=work, daemon=True).start()
