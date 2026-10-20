"""Real Chromium, simulated client/OCR: the no-sampling end-user workflow."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from playwright.sync_api import sync_playwright,expect
from umacoach.session import Session
from umacoach.webapp import Server
from test_automatic import fixture,ocr


def main():
    with tempfile.TemporaryDirectory() as tmp:
        session=Session(Path(tmp));current=[fixture()];stop=threading.Event()
        with patch('umacoach.webapp.windows',return_value=[(123,'賽馬娘Pretty Derby')]),patch('umacoach.session.windows',return_value=[(123,'賽馬娘Pretty Derby')]),patch('umacoach.session.capture',side_effect=lambda _:current[0][0]),patch.object(session.local_coach.ocr,'recognize',side_effect=lambda image,regions:ocr(image,current[0][1],regions)),patch('umacoach.session.codex_backend.analyze_codex') as model:
            server=Server(('127.0.0.1',0),session);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            def sample():
                while not stop.wait(.1):session.tick()
            threading.Thread(target=sample,daemon=True).start()
            try:
                with sync_playwright() as p:
                    opts=dict(headless=True,args=['--no-sandbox']);executable=shutil.which('chromium') or shutil.which('chromium-browser')
                    if executable:opts['executable_path']=executable
                    browser=p.chromium.launch(**opts);page=browser.new_page(viewport={'width':1500,'height':1200});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(server.origin);page.locator('#window').select_option('123');page.locator('#start-auto').click()
                    expect(page.locator('#local-ranking')).to_contain_text('速度：',timeout=10000)
                    expect(page.locator('#local-date')).to_contain_text('古典年5月前半')
                    expect(page.locator('#automatic-status')).to_contain_text('已定位界面')
                    assert session.tracker.active and not session.tracker.pending
                    assert not page.locator('#advanced-vision').get_attribute('open')
                    assert not page.locator('#ai-settings').get_attribute('open')
                    assert not (Path(tmp)/'templates/index.json').exists()
                    for name in ['耐力','力量','毅力','智力']:
                        current[0]=fixture(name)
                        expect(page.locator('#local-ranking')).to_contain_text(name+'：',timeout=10000)
                    expect(page.locator('#local-advice')).to_contain_text('本回合选')
                    model.assert_not_called()
                    current[0]=fixture('速度','古典年5月後半')
                    expect(page.locator('#local-date')).to_contain_text('古典年5月後半',timeout=10000)
                    expect(page.locator('#local-ranking')).not_to_contain_text('耐力：')
                    page.get_by_role('button',name='记录为新局',exact=True).click()
                    expect(page.locator('#local-ranking')).not_to_contain_text('速度：')
                    assert session.tracker.active.local=={}
                    assert not errors,errors
                    browser.close();print('Chromium passed: select window + one start; automatic run attachment, five-tab local comparison, no sampling/configuration/login/model, date rollover and new-run isolation')
            finally:
                stop.set();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':main()
