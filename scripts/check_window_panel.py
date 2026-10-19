"""Optional browser regression: python scripts/check_window_panel.py.

Requires Pillow, playwright and a local Chromium executable. Uses simulated
capture/analysis only, never authenticates or consumes model quota.
"""
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from playwright.sync_api import sync_playwright, expect
from umacoach.session import Session
from umacoach.webapp import Server


def observation():
    return dict(scene='run_start', confidence=.99, character='貴婦人', date_text='', turn_index=None,
        evidence=['模拟开局'], recommendation='查看当前训练预览', reason='测试画面',
        missing=[], alternatives=[], guide_refs=[])


def main():
    executable=shutil.which('chromium') or shutil.which('chromium-browser')
    with tempfile.TemporaryDirectory() as tmp, \
         patch('umacoach.webapp.windows',return_value=[(123,'赛马娘'),(456,'另一个游戏窗口')]), \
         patch('umacoach.session.windows',return_value=[(123,'赛马娘'),(456,'另一个游戏窗口')]), \
         patch('umacoach.session.capture',return_value=Image.new('RGB',(300,500),'green')) as capture, \
         patch('umacoach.session.capture_region',return_value=Image.new('RGB',(100,200),'blue')) as region, \
         patch('umacoach.session.codex_backend.analyze_codex',return_value=observation()) as analyze:
        session=Session(Path(tmp));session.codex_ready=True;session.codex_state='ready'
        server=Server(('127.0.0.1',0),session)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                options=dict(headless=True,args=['--no-sandbox'])
                if executable:options['executable_path']=executable
                browser=p.chromium.launch(**options)
                page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(server.origin)
                expect(page.locator('#read')).to_be_enabled()
                page.locator('#read').click()
                expect(page.locator('#error')).to_contain_text('请先在上方选择游戏窗口')
                assert session.hwnd is None
                analyze.assert_not_called()
                # Match the screenshot: account ready, choice made, never apply.
                page.locator('#window').select_option('123')
                expect(page.locator('#target')).to_contain_text('点击读取将自动应用')
                page.locator('#refresh').click()
                expect(page.locator('#window')).to_have_value('123')
                page.locator('#provider').select_option('api')
                page.locator('#endpoint').fill('');page.locator('#model').fill('')
                page.locator('#provider').select_option('codex')
                page.locator('#read').click()
                expect(page.locator('#advice')).to_contain_text('查看当前训练预览')
                expect(page.locator('#target')).to_contain_text('当前读取目标：赛马娘')
                expect(page.locator('#preview')).to_be_visible()
                assert session.hwnd==123
                capture.assert_called_with(123)
                page.locator('#window').select_option('456')
                page.locator('#auto').click()
                expect(page.locator('#auto')).to_have_text('暂停读取')
                assert session.hwnd==456 and session.auto
                page.locator('#auto').click()
                expect(page.locator('#auto')).to_have_text('开始持续读取')
                # Existing manual crop must survive the automatic-apply helper.
                with session.lock:
                    session.hwnd=None;session.region=(0,0,100,200);session.target_label='手动框选区域'
                page.reload()
                expect(page.locator('#window')).to_have_value('region')
                page.locator('#read').click()
                expect(page.locator('#advice')).to_contain_text('查看当前训练预览')
                region.assert_called_with((0,0,100,200))
                assert session.region==(0,0,100,200) and session.hwnd is None
                assert not errors,errors
                browser.close()
                print('Chromium passed: missing selection, pending choice, refresh preservation, '
                      'read auto-apply, hidden API blanks, start/pause auto, existing crop; no JS errors')
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':main()
