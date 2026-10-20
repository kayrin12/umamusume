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
from copy import deepcopy

from PIL import Image

from . import __version__
from . import codex_backend

from .capture import (CaptureError, Gate, capture, capture_region, desktop_snapshot,
                      distance, foreground_window, selection_box, windows)
from .tracker import Tracker
from .vision import analyze
from .run_state import StateError, context_state, merge_state
from .local_coach import LocalCoach


ROOT = Path(__file__).resolve().parent.parent


class ActionError(ValueError):
    """An explicitly written, credential-free message safe for the panel."""


class Session:
    def __init__(self, data_dir: Path):
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self.tracker = Tracker(data_dir/'runs.json')
        self.key = os.environ.get('UMACOACH_API_KEY', '')
        self.endpoint = os.environ.get('UMACOACH_API_URL', 'https://api.openai.com/v1')
        self.model = os.environ.get('UMACOACH_MODEL', 'gpt-4.1-mini')
        self.provider = 'codex'
        self.codex_ready = False
        self.codex_busy = False
        self.codex_state = 'unchecked'
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
        self.previewed_at = 0
        self.template_capture = None
        self.preview = ''
        self.sample = False
        self.history = []
        self.result = None
        self.status = '选择游戏窗口，点击“应用配置”，然后开始读取。未配置 API 也能本地预览。'
        self.local_coach = LocalCoach(self)

    def snapshot(self):
        with self.lock:
            run = self.tracker.active
            return dict(status=self.status, auto=self.auto, busy=self.busy,
                        local=self.local_coach.snapshot(),
                        provider=self.provider, analysis_ready=self.analysis_ready(),
                        codex_ready=self.codex_ready, codex_busy=self.codex_busy, codex_state=self.codex_state,
                        has_key=bool(self.key), endpoint=self.endpoint, model=self.model,
                        hwnd=self.hwnd, target_label=self.target_label, region=self.region,
                        picking=bool(self.picker), preview=self.preview, result=self.result,
                        pending=self.tracker.pending, sample=self.sample,
                        run_id=run.id if run else None,
                        run_state=context_state(run.state) if run else None,
                        state_revision=run.state_revision if run else None,
                        character=run.character if run else '',
                        run_count=len(self.tracker.runs))

    def correct_state(self, run_id, revision, values):
        with self.lock:
            run = self.tracker.active
            if not run or self.tracker.pending or self.sample or run.id != run_id:
                raise StateError('请先确认当前局次；样本模式不能修改真实局次状态')
            if type(revision) is not int or revision != run.state_revision:
                raise StateError('状态已更新，请重新打开状态面板后修正，避免覆盖新记录')
            updated, changed = merge_state(run.state, values, manual=True, turn=run.last_turn)
            if changed:
                run.state, run.state_revision = updated, run.state_revision+1
                run.local['training'] = {}
                self.tracker.save()
                self.epoch += 1
                self.auto = False
                self.result, self.history = None, []
                self.gate = Gate()
                self.status = '本局状态已修正并保存；已暂停自动读取，请重新分析当前画面。'

    def configure(self, data):
        with self.lock:
            provider = data.get('provider', self.provider)
            if provider not in ('codex', 'api'):
                raise ActionError('请选择 ChatGPT 账号或视觉 API')
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
            if provider == 'api' and (not endpoint or not model):
                raise ActionError('请填写视觉 API 地址和模型；尚无密钥也能使用本地预览')
            if provider == 'codex':
                endpoint, model = endpoint or self.endpoint, model or self.model
            self.hwnd, self.endpoint, self.model = hwnd, endpoint, model
            self.provider = provider
            self.region, self.target_label = region, label
            self.picker = None
            if data.get('key', '').strip():
                self.key = data['key'].strip()
            self.auto = False
            self.epoch += 1
            self.gate = Gate()
            self.frame, self.preview, self.sample = None, '', False
            self.history, self.result = [], None
            self.local_coach.stable.previous = {}
            self.local_coach.live_scene = 'unknown'
            self.local_coach.live_date = None
            self.status = '配置已应用。'+('ChatGPT 账号模式，请完成官方登录并检查状态。' if provider == 'codex' and not self.codex_ready else '可以开始读取画面。')

    def analysis_ready(self):
        return self.codex_ready if self.provider == 'codex' else bool(self.key)

    def set_provider(self, provider):
        if provider not in ('codex', 'api'):
            raise ActionError('请选择支持的分析方式')
        with self.lock:
            self.provider = provider
            self.auto = False
            self.epoch += 1
            self.result, self.history = None, []
            self.status = '已切换为'+('ChatGPT 账号（Codex 额度）' if provider == 'codex' else '视觉 API')+'模式。'

    def check_codex(self):
        state = codex_backend.login_status()
        with self.lock:
            self.codex_state = state
            self.codex_ready = state == 'ready'
            self.status = {'ready': 'ChatGPT 登录已确认，将使用账号 Codex 额度分析画面。',
                           'not_installed': '尚未安装 Codex 组件，请点“使用 ChatGPT 登录”；首次自动下载约162MB。',
                           'login_required': '尚未确认 ChatGPT 登录，请完成官方登录后再检查。',
                           'unavailable': 'Codex 组件未能响应，请重试或重新启动助手。'}[state]

    def login_codex(self):
        with self.lock:
            if self.codex_busy:
                raise ActionError('正在准备登录，请稍候')
            self.auto = False
            self.epoch += 1
            self.result = None
            self.codex_busy, self.codex_ready = True, False
            self.codex_state = 'preparing'
            self.status = '正在准备官方 Codex 组件与 ChatGPT 登录…'
        def progress(message):
            with self.lock:
                self.status = message
                self.codex_state = 'installing'
        def work():
            try:
                if not codex_backend.binary():
                    codex_backend.install(progress)
                codex_backend.open_login()
                with self.lock:
                    self.codex_state = 'login_required'
                    self.status = '官方登录窗口已打开。完成 ChatGPT 登录后，回到此处点“检查登录状态”。'
            except codex_backend.CodexError as error:
                with self.lock:
                    self.codex_state = 'unavailable'
                    self.status = str(error)
            except Exception:
                with self.lock:
                    self.codex_state = 'unavailable'
                    self.status = 'Codex 组件准备或登录启动失败，请检查网络、磁盘空间后重试。'
            finally:
                with self.lock:
                    self.codex_busy = False
        threading.Thread(target=work, daemon=True).start()

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
                        provider=self.provider, codex_state=self.codex_state, analysis_ready=self.analysis_ready(),
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
            self.auto = False
            self.epoch += 1
            self.gate = Gate()
            self.history, self.result = [], None
            self.local_coach.stable.previous = {}
            self.local_coach.live_scene = 'unknown'
            self.local_coach.live_date = None
            if kind == 'new':
                self.tracker.new()
            elif kind == 'resume':
                if self.tracker.active:
                    self.tracker.active.last_turn = None
                    if not self.tracker.active.character_locked:
                        self.tracker.active.character = ''
                    self.tracker.active.local['training'] = {}
                self.tracker.resume()
                self.tracker.save()
            elif kind == 'abandon':
                self.tracker.close('manual_abandon')
            else:
                raise ActionError('未知局次操作')
            self.status = '局次已更新，旧建议已清除。'

    def set_frame(self, frame):
        self.frame = frame.copy()
        if self.preview and time.monotonic()-self.previewed_at<.25:return
        self.previewed_at=time.monotonic()
        thumb = frame.convert('RGB')
        thumb.thumbnail((420, 640))
        out = BytesIO()
        thumb.save(out, format='JPEG', quality=75)
        self.preview = base64.b64encode(out.getvalue()).decode()

    def import_image(self, data):
        if len(data) > 12_000_000:
            raise ActionError('图片过大，请使用小于8MB的截图')
        raw = base64.b64decode(data, validate=True)
        with Image.open(BytesIO(raw)) as image:
            if image.width*image.height > 24_000_000:
                raise ActionError('图片尺寸过大')
            frame = image.convert('RGB')
        with self.lock:
            if self.busy:
                raise ActionError('请等待当前分析完成再导入')
            self.epoch += 1
            self.auto, self.sample = False, True
            self.result = None
            self.set_frame(frame)
            self.status = '样本已载入；样本分析不会修改真实局次。'
            if self.analysis_ready():
                self.submit(frame, True)

    def read_now(self, force_model=False):
        with self.lock:
            if self.busy and (force_model or not self.local_coach.settings['enabled'] or self.sample):
                raise ActionError('上一张画面仍在分析，请稍候')
            if self.sample and self.frame is not None:
                frame = self.frame.copy()
            else:
                if self.hwnd is None and self.region is None:
                    raise ActionError('请先在上方选择游戏窗口并应用配置，或使用前台选择、手动框选、导入截图')
                try:
                    frame = self.read_frame()
                    self.capture_error = ''
                except CaptureError as error:
                    self.capture_error = str(error)
                    self.status = str(error)
                    self.result = None
                    raise
                self.set_frame(frame)
            if self.local_coach.settings['enabled'] and not force_model and not self.sample:
                self.local_coach.submit(frame,force=True)
                self.status = '正在本地读取；无需等待模型。事件或心得可点“模型分析当前画面”。'
                return
            if not self.analysis_ready():
                self.result = None
                self.status = '画面预览正常。'+('请点“使用 ChatGPT 登录”，完成后检查登录状态，即可获取建议。' if self.provider == 'codex' else '请填写视觉 API 密钥以获取建议。')
                return
            self.submit(frame, self.sample)

    def toggle(self, enabled):
        with self.lock:
            if enabled and self.hwnd is None and self.region is None:
                raise ActionError('请先选择游戏窗口并应用配置')
            self.auto = enabled
            self.epoch += 1
            self.gate = Gate()
            self.result = None
            if enabled:
                self.sample = False
            self.status = ('正在读取可见游戏窗口并等待分析。' if self.analysis_ready() else '正在本地预览；请完成 ChatGPT 登录或配置 API 后再分析。') if enabled else ('已暂停读取；正在进行的分析可能仍消耗额度。' if self.busy else '已暂停读取。')
            if enabled and self.local_coach.settings['enabled']:
                self.status = '正在本地持续读取；切换训练预览即可自动缓存，无需逐项点读取。模型用于事件、心得与局次边界确认。'

    def tick(self):
        with self.lock:
            if not self.auto:
                return
            try:
                frame = self.read_frame()
                self.set_frame(frame)
                if self.local_coach.settings['enabled']:
                    self.local_coach.submit(frame)
                    return
                ready = self.gate.ready(frame, time.monotonic())
                interval = 30 if self.provider == 'codex' else 8
                if ready and not self.busy and self.analysis_ready() and time.monotonic()-self.requested_at >= interval:
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
            raise ActionError('上一张画面仍在分析')
        self.busy = True
        self.requested_at = time.monotonic()
        epoch, endpoint, key, model = self.epoch, self.endpoint, self.key, self.model
        provider = self.provider
        run = self.tracker.active
        context = {} if sample else dict(run_id=run.id if run else None,
                                        pending_boundary=self.tracker.pending,
                                        run_state=context_state(run.state) if run and not self.tracker.pending else None,
                                        recent_observations=self.history[-4:])
        if not sample:
            context['fixed_character'] = run.character if run and run.character_locked else None
            context['local_observations'] = self.local_coach.snapshot()
        context = deepcopy(context)
        self.result = None
        self.status = '正在分析截图样本…' if sample else '正在分析当前游戏画面…'

        def work():
            try:
                if provider == 'codex':
                    result = codex_backend.analyze_codex(frame, context, ROOT/'guides/legendary_tw.md')
                else:
                    result = analyze(frame, endpoint, key, model, context, ROOT/'guides/legendary_tw.md')
                with self.lock:
                    if epoch != self.epoch:
                        return
                    if not sample and self.auto and self.frame is not None and distance(self.frame, frame) > 12:
                        self.status = '分析期间画面已明显变化，过时结果已丢弃。'
                        return
                    self.gate.mark(frame, time.monotonic())
                    active = self.tracker.active
                    if (active and active.local.get('date',{}).get('source') in ('local_ocr_twice','self_templates')
                            and result.get('turn_index') is not None and active.last_turn is not None
                            and result['turn_index'] < active.last_turn and not self.tracker.pending):
                        self.status = '模型结果来自更早回合，已丢弃；继续使用当前本地记录。'
                        return
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
            except StateError as error:
                with self.lock:
                    if epoch == self.epoch:
                        self.auto, self.result = False, None
                        self.status = '状态更新未采纳：'+str(error)+'。请查看并修正本局状态后重试。'
            except codex_backend.CodexError as error:
                with self.lock:
                    if epoch == self.epoch:
                        self.auto, self.result = False, None
                        self.status = str(error)
                        if error.reason == 'login':
                            self.codex_ready = False
                            self.codex_state = 'login_required'
            except Exception:
                with self.lock:
                    if epoch == self.epoch:
                        self.auto = False
                        self.result = None
                        # Neither API errors nor logs expose entered keys or request data.
                        self.status = ('Codex 分析失败，已暂停。请检查组件、ChatGPT 登录和网络后重试。' if provider == 'codex' else '分析失败，已暂停。请检查 API 地址、模型、密钥、配额和网络后重试。')
            finally:
                with self.lock:
                    self.busy = False

        threading.Thread(target=work, daemon=True).start()
