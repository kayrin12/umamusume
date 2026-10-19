"""Optional Chromium flow with simulated OCR/capture. No model credentials used."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from playwright.sync_api import sync_playwright,expect
from umacoach.session import Session
from umacoach.webapp import Server
from umacoach.schedule import catalog


def ocr_fixture(kind,date):
    labels=['速度','耐力','力量','毅力','智力','技能Pt']
    gains=[20,0,8,0,0,20] if kind=='速度' else [0,30,8,0,0,40]
    words=[dict(text=label,x=20+i*100,y=10,w=40,h=20) for i,label in enumerate(labels)]
    words += [dict(text='+'+str(n),x=20+i*100,y=40,w=40,h=20) for i,n in enumerate(gains)]
    return dict(items=[dict(key='header',text=date+' 還有3回合'),dict(key='selected',text=kind),
        dict(key='preview',text=kind+'訓練 失敗率0%',words=words)],elapsed_ms=35,language='zh-Hant')


def main():
    current=dict(kind='速度',date='古典年5月前半')
    with tempfile.TemporaryDirectory() as tmp,patch('umacoach.webapp.windows',return_value=[(123,'赛马娘')]),patch('umacoach.session.windows',return_value=[(123,'赛马娘')]),patch('umacoach.session.capture',return_value=Image.new('RGB',(700,1000),'green')),patch('umacoach.session.codex_backend.analyze_codex') as ai:
        session=Session(Path(tmp));stop=threading.Event()
        with patch.object(session.local_coach.ocr,'recognize',side_effect=lambda *args:ocr_fixture(**current)):
            server=Server(('127.0.0.1',0),session)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            def sample():
                while not stop.wait(.2):session.tick()
            threading.Thread(target=sample,daemon=True).start()
            try:
                with sync_playwright() as p:
                    options=dict(headless=True,args=['--no-sandbox'])
                    executable=shutil.which('chromium') or shutil.which('chromium-browser')
                    if executable:options['executable_path']=executable
                    browser=p.chromium.launch(**options);page=browser.new_page(viewport={'width':1400,'height':1100});errors=[]
                    page.on('pageerror',lambda e:errors.append(str(e)));page.goto(server.origin)
                    page.locator('#window').select_option('123');page.get_by_role('button',name='记录为新局',exact=True).click()
                    page.locator('#local-enabled').check();page.locator('#local-save-settings').click()
                    page.get_by_text('固定本局角色与日期 / 粉丝快速修正',exact=True).click()
                    page.locator('#local-character').locator('option').filter(has_text='貴婦人').first.wait_for(state='attached')
                    profile=next(x for x in catalog()['profiles'] if x['name']=='貴婦人')
                    page.locator('#local-character').select_option(profile['id']);page.locator('#local-fix-character').click()
                    page.locator('#auto').click()
                    expect(page.locator('#local-ranking')).to_contain_text('速度：',timeout=10000)
                    expect(page.locator('#local-date')).to_contain_text('貴婦人（已固定）')
                    expect(page.locator('#local-date')).to_contain_text('古典年5月前半')
                    current['kind']='耐力'
                    expect(page.locator('#local-ranking')).to_contain_text('耐力：',timeout=10000)
                    expect(page.locator('#local-advice')).to_contain_text('优先点「耐力」')
                    assert len(session.tracker.active.local['training'])==2
                    ai.assert_not_called()
                    page.locator('#local-fans').fill('1000');page.locator('#local-save-fans').click()
                    page.get_by_text('登记客户端已确认的目标门槛',exact=True).click()
                    page.locator('#goal-name').fill('测试报名门槛');page.locator('#goal-year').select_option('2');page.locator('#goal-month').fill('6');page.locator('#goal-half').select_option('0');page.locator('#goal-fans').fill('6000');page.locator('#goal-add').click()
                    expect(page.locator('#local-alerts')).to_contain_text('仍差5000')
                    expect(page.locator('#local-advice')).to_contain_text('先处理粉丝门槛')
                    page.locator('#local-turn').fill('23');page.locator('#local-anchor').click()
                    expect(page.locator('#local-date')).to_contain_text('本局第 23 回合')
                    current['date']='古典年5月後半'
                    expect(page.locator('#local-date')).to_contain_text('本局第 24 回合',timeout=10000)
                    expect(page.locator('#local-ranking')).not_to_contain_text('速度：')
                    # Manually entered preview does not require OCR or a model.
                    page.locator('#auto').click()
                    page.get_by_text('当前训练快速录入（OCR不完整时使用）',exact=True).click()
                    page.locator('#quick-kind').select_option('智力')
                    for i,value in enumerate([0,0,0,0,20,35]):page.locator('#gain-'+str(i)).fill(str(value))
                    page.locator('#quick-failure').fill('0');page.locator('#quick-save').click()
                    expect(page.locator('#local-ranking')).to_contain_text('智力：')
                    # Calibration persists relative coordinates, without external analysis.
                    page.locator('#local-calibrate').click();expect(page.locator('#ocr-dialog')).to_be_visible()
                    box=page.locator('#ocr-canvas').bounding_box();page.mouse.move(box['x']+20,box['y']+20);page.mouse.down();page.mouse.move(box['x']+box['width']*.8,box['y']+box['height']*.2);page.mouse.up()
                    page.locator('#ocr-set').click();expect(page.locator('#ocr-error')).to_contain_text('已保存')
                    page.locator('#ocr-close').click();assert session.local_coach.path.exists()
                    page.get_by_role('button',name='记录为新局',exact=True).click()
                    expect(page.locator('#local-date')).to_contain_text('请固定本局角色')
                    assert session.tracker.active.local=={};assert not session.tracker.active.character_locked
                    assert not errors,errors
                    browser.close()
                    print('Chromium passed: fixed role, automatic OCR cache without model, ranking, fan warning, actual-turn anchor, next-turn invalidation, quick entry, ROI calibration and new-run reset')
            finally:
                stop.set();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':main()
