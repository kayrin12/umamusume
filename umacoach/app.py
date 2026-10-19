import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from PIL import Image, ImageTk

from .capture import Gate, capture, distance, windows
from .tracker import Tracker
from .vision import analyze


ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('UMACOACH_DATA_DIR', ROOT / 'local-data'))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        family = 'Microsoft YaHei UI' if sys.platform == 'win32' else 'Noto Sans CJK SC'
        for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont'):
            tkfont.nametofont(name).configure(family=family, size=10)
        self.title('赛马娘繁中服 · 只读养成助手（原型）')
        self.geometry('1060x820')
        self.tracker = Tracker(DATA / 'runs.json')
        self.events = queue.Queue()
        self.gate = Gate()
        self.busy = False
        self.requested_at = float('-inf')
        self.auto = False
        self.epoch = 0
        self.history = []
        self.targets = []
        self.image = None
        self.shown = None
        self.api = tk.StringVar(value=os.environ.get('UMACOACH_API_URL', 'https://api.openai.com/v1'))
        self.model = tk.StringVar(value=os.environ.get('UMACOACH_MODEL', 'gpt-4.1-mini'))
        self.key = tk.StringVar(value=os.environ.get('UMACOACH_API_KEY', ''))
        self.topmost = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='先选择游戏窗口；截图仅在发起分析时上传到所配置的 API。')
        box = ttk.Frame(self, padding=10)
        box.pack(fill='both', expand=True)
        for row, (label, variable, masked) in enumerate([
            ('视觉 API 根地址', self.api, False), ('支持视觉的模型', self.model, False), ('API 密钥（仅内存）', self.key, True)]):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky='w')
            ttk.Entry(box, textvariable=variable, show='*' if masked else '').grid(row=row, column=1, columnspan=4, sticky='ew', pady=2)
        ttk.Label(box, text='游戏窗口').grid(row=3, column=0, sticky='w')
        self.window = ttk.Combobox(box, state='readonly')
        self.window.grid(row=3, column=1, columnspan=3, sticky='ew')
        self.window.bind('<<ComboboxSelected>>', lambda _: self.change_target())
        ttk.Button(box, text='刷新窗口', command=self.refresh_windows).grid(row=3, column=4)
        toolbar = ttk.Frame(box)
        toolbar.grid(row=4, column=0, columnspan=5, sticky='ew', pady=8)
        self.auto_button = ttk.Button(toolbar, text='开始持续读取', command=self.toggle_auto)
        self.auto_button.pack(side='left')
        ttk.Button(toolbar, text='立即分析（F8）', command=self.manual).pack(side='left', padx=4)
        ttk.Button(toolbar, text='导入截图', command=self.import_image).pack(side='left')
        ttk.Checkbutton(toolbar, text='置顶', variable=self.topmost,
                        command=lambda: self.attributes('-topmost', self.topmost.get())).pack(side='left', padx=8)
        runs = ttk.Frame(box)
        runs.grid(row=5, column=0, columnspan=5, sticky='ew')
        ttk.Button(runs, text='记录为新局', command=lambda: self.boundary('new')).pack(side='left')
        ttk.Button(runs, text='继续当前局', command=lambda: self.boundary('resume')).pack(side='left', padx=4)
        ttk.Button(runs, text='本局已放弃', command=lambda: self.boundary('abandon')).pack(side='left')
        self.run_label = ttk.Label(runs)
        self.run_label.pack(side='left', padx=12)
        ttk.Label(box, textvariable=self.status, wraplength=990).grid(row=6, column=0, columnspan=5, sticky='w', pady=8)
        content = ttk.Panedwindow(box, orient='horizontal')
        content.grid(row=7, column=0, columnspan=5, sticky='nsew')
        self.preview = ttk.Label(content, anchor='center', text='画面预览：请确保游戏窗口可见、未被遮挡')
        self.output = tk.Text(content, wrap='word', font=(family, 11), width=62)
        content.add(self.preview, weight=1)
        content.add(self.output, weight=2)
        self.output.insert('end', '此原型尚未核验当前攻略，不能保证每回合最优。\n\n'
            '自动模式每秒在本地检查画面；画面稳定且改变时才调用视觉 API，至少间隔 8 秒。\n'
            'API 可能收费；不会持续上传视频，也不保存原始截图。\n\n'
            '选择游戏窗口并填写 API 后点击开始。导入截图只做离线样本分析，不影响真实局次。\n'
            '只看见一个训练预览时，助手会提示继续查看其他训练，不会虚构收益。')
        box.columnconfigure(1, weight=1)
        box.rowconfigure(7, weight=1)
        self.bind('<F8>', lambda _: self.manual())
        self.refresh_windows()
        self.update_run_label()
        self.after(200, self.tick)

    def refresh_windows(self):
        self.targets = [(h, title) for h, title in windows() if h != self.winfo_id() and '只读养成助手' not in title]
        self.window['values'] = [f'{title} [{handle}]' for handle, title in self.targets]
        self.window.set('')
        self.change_target()

    def change_target(self):
        self.auto = False
        self.auto_button.config(text='开始持续读取')
        self.epoch += 1
        self.gate = Gate()
        self.history = []
        self.image = None

    def selected(self):
        index = self.window.current()
        if index < 0 or index >= len(self.targets):
            raise ValueError('请先从列表中选择繁中服游戏窗口')
        return self.targets[index][0]

    def show_image(self, frame):
        thumb = frame.copy()
        thumb.thumbnail((330, 520))
        self.shown = ImageTk.PhotoImage(thumb)
        self.preview.configure(image=self.shown, text='')

    def update_run_label(self):
        run = self.tracker.active
        text = '待确认局次' if self.tracker.pending else '无进行中的局次'
        if run:
            text += f' · {run.id} · {run.character or "角色待识别"}'
        self.run_label.configure(text=text)

    def boundary(self, kind):
        self.epoch += 1  # Discard in-flight responses from the previous run.
        self.gate = Gate()
        self.history = []
        if kind == 'new':
            self.tracker.new()
        elif kind == 'resume':
            # Reconcile a date rollback only by explicit user action.
            if self.tracker.active:
                self.tracker.active.last_turn = None
                self.tracker.active.character = ''
            self.tracker.resume()
            self.tracker.save()
        else:
            self.tracker.close('manual_abandon')
        self.update_run_label()
        self.output.delete('1.0', 'end')
        self.output.insert('end', '局次已更新，旧建议已清除。等待下一次画面分析。')

    def toggle_auto(self):
        if not self.auto:
            try:
                self.selected()
                if not self.key.get().strip():
                    raise ValueError('请先填写视觉 API 密钥，或设置 UMACOACH_API_KEY')
            except ValueError as e:
                messagebox.showinfo('需要配置', str(e))
                return
            self.auto = True
            self.status.set('持续读取中：请让游戏窗口保持可见。可随时暂停。')
        else:
            self.auto = False
            self.epoch += 1
            self.status.set('已暂停读取；正在进行的 API 请求可能仍计费，返回结果将丢弃。')
        self.auto_button.config(text='暂停读取' if self.auto else '开始持续读取')

    def manual(self):
        if self.busy:
            self.status.set('上一帧还在分析，请稍候。')
            return
        try:
            frame = capture(self.selected())
            self.image = frame
            self.show_image(frame)
            self.submit(frame, False)
        except Exception as e:
            self.status.set(str(e))

    def import_image(self):
        path = filedialog.askopenfilename(filetypes=[('图片', '*.png *.jpg *.jpeg *.webp')])
        if not path:
            return
        if self.busy:
            self.status.set('请等待当前分析完成再导入截图。')
            return
        self.auto = False
        self.epoch += 1
        self.auto_button.config(text='开始持续读取')
        try:
            with Image.open(path) as pic:
                self.image = pic.convert('RGB')
            self.show_image(self.image)
            self.submit(self.image, True)
        except Exception as e:
            self.status.set(str(e))

    def submit(self, frame, sample):
        key, endpoint, model = self.key.get().strip(), self.api.get().strip(), self.model.get().strip()
        if not key:
            self.status.set('画面已捕获；尚未分析，请配置视觉 API 密钥。')
            return
        if not model:
            self.status.set('请填写支持图片输入和 JSON 输出的模型名。')
            return
        self.busy = True
        self.requested_at = time.monotonic()
        epoch = self.epoch
        run = self.tracker.active
        context = {} if sample else {
            'run_id': run.id if run else None, 'pending_boundary': self.tracker.pending,
            'recent_observations': self.history[-4:]}
        self.status.set('正在分析截图样本…' if sample else '正在分析当前游戏画面…')
        self.output.delete('1.0', 'end')
        self.output.insert('end', '正在读取新画面，旧建议已收起。请等待本次分析完成。')
        def work():
            try:
                result = analyze(frame, endpoint, key, model, context, ROOT/'guides/legendary_tw.md')
                self.events.put((epoch, sample, result, None, frame))
            except Exception as e:
                self.events.put((epoch, sample, None, f'{type(e).__name__}: {e}', frame))
        threading.Thread(target=work, daemon=True).start()

    def receive(self):
        try:
            epoch, sample, result, error, frame = self.events.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        if epoch != self.epoch:
            return
        if error:
            self.status.set('分析失败；自动读取已暂停。'+error)
            self.auto = False
            self.auto_button.config(text='开始持续读取')
            return
        if not sample and self.auto and self.image is not None and distance(self.image, frame) > 12:
            self.status.set('分析期间画面已明显变化，已丢弃过时结果，等待新画面。')
            return
        self.gate.mark(frame, time.monotonic())
        prior = self.tracker.active.id if self.tracker.active else None
        notice = '截图样本模式，不修改真实局次' if sample else self.tracker.apply(result)
        current = self.tracker.active.id if self.tracker.active else None
        if not sample:
            if current != prior or self.tracker.pending:
                self.history = []
            if not self.tracker.pending and self.tracker.active:
                self.history.append(result)
                self.history = self.history[-4:]
            if not self.tracker.pending:
                DATA.mkdir(parents=True, exist_ok=True)
                with (DATA/'observations.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'at': time.time(), 'run_id': current or prior, 'observation': result}, ensure_ascii=False)+'\n')
        self.update_run_label()
        self.status.set(notice)
        self.output.delete('1.0', 'end')
        if self.tracker.pending and not sample:
            self.output.insert('end', notice+'\n\n确认后才会恢复本局建议。')
            return
        if result['confidence'] < .85:
            self.output.insert('end', '画面识别置信度不足，暂不显示行动建议。请等待画面稳定或手动刷新。\n\n')
        else:
            self.output.insert('end', '下一步：'+result['recommendation']+'\n\n原因：'+result['reason']+'\n\n')
        for label, field in [('画面依据', 'evidence'), ('缺失信息', 'missing'), ('其他选项', 'alternatives'), ('攻略引用', 'guide_refs')]:
            self.output.insert('end', label+'：\n'+('\n'.join('• '+x for x in result[field]) or '暂无')+'\n\n')
        self.output.insert('end', f'画面：{result["scene"]}；日期：{result["date_text"] or "未知"}\n'
                           '提示：未核验当前攻略，建议不代表全局最优；训练收益以游戏实际显示为准。')

    def tick(self):
        self.receive()
        if self.auto:
            try:
                frame = capture(self.selected())
                self.image = frame
                self.show_image(frame)
                ready = self.gate.ready(frame, time.monotonic())
                if ready and not self.busy and time.monotonic()-self.requested_at >= 8:
                    self.submit(frame, False)
            except Exception as e:
                self.auto = False
                self.auto_button.config(text='开始持续读取')
                self.status.set(str(e))
        self.after(1000, self.tick)


def main():
    if sys.platform == 'win32':
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    App().mainloop()


if __name__ == '__main__':
    main()
