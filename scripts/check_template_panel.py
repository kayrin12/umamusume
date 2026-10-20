"""Real browser, synthetic fixtures: sample capture, correction and fast rankings."""
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright,expect
from umacoach.session import Session
from umacoach.webapp import Server
from scripts.benchmark_templates import fixture


def main():
    with tempfile.TemporaryDirectory() as tmp:
        session=Session(Path(tmp));image,regions=fixture(session.local_coach.fast.bank)
        frame=[image];stop=threading.Event()
        with patch('umacoach.webapp.windows',return_value=[(123,'赛马娘')]),patch('umacoach.session.windows',return_value=[(123,'赛马娘')]),patch('umacoach.session.capture',side_effect=lambda _:frame[0]),patch('umacoach.session.codex_backend.analyze_codex') as model:
            server=Server(('127.0.0.1',0),session);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            def sample():
                while not stop.wait(.1):session.tick()
            threading.Thread(target=sample,daemon=True).start()
            try:
                with sync_playwright() as p:
                    opts=dict(headless=True,args=['--no-sandbox']);executable=shutil.which('chromium') or shutil.which('chromium-browser')
                    if executable:opts['executable_path']=executable
                    browser=p.chromium.launch(**opts);page=browser.new_page(viewport={'width':1500,'height':1200});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(server.origin);page.locator('#window').select_option('123');page.get_by_role('button',name='记录为新局',exact=True).click()
                    page.locator('#advanced-vision > summary').click();page.locator('#fast-backend').select_option('templates');page.locator('#fast-backend-save').click();page.locator('#auto').click()
                    expect(page.locator('#local-ranking')).to_contain_text('速度：',timeout=10000)
                    expect(page.locator('#fast-cards')).to_contain_text('卡槽1：A')
                    expect(page.locator('#template-performance')).to_contain_text('300 ms目标')
                    assert session.local_coach.fast.live['counts']['friends']==1
                    model.assert_not_called()
                    # No model, no manual read when switching to another sampled tab.
                    from scripts.benchmark_templates import pattern
                    for name,color,mark in [('耐力','blue',1),('力量','green',2),('毅力','orange',3),('智力','purple',4)]:
                        changed=frame[0].copy();r=regions['selected'];box=[round(r[i]*[changed.width,changed.height][i%2]) for i in range(4)];changed.paste(pattern(color,mark),box[:2]);frame[0]=changed
                        expect(page.locator('#local-ranking')).to_contain_text(name+'：',timeout=10000)
                    expect(page.locator('#local-advice')).to_contain_text('本回合选')
                    page.locator('#auto').click()
                    page.get_by_text('本帧各字段识别结果 / 手动修正',exact=True).click()
                    page.locator('#correct-field').select_option('gain_speed');page.locator('#correct-value').fill('21');page.locator('#correct-save').click()
                    expect(page.locator('#fast-values')).to_contain_text('速度 +收益：21',timeout=5000)
                    # Editable policy is JSON, never interpreted code.
                    page.locator('#weights-open').click();expect(page.locator('#weights-dialog')).to_be_visible()
                    import json
                    policy=json.loads(page.locator('#weights-json').input_value());policy['failure_expected_loss']=99
                    page.locator('#weights-json').fill(json.dumps(policy,ensure_ascii=False));page.locator('#weights-save').click();expect(page.locator('#weights-error')).to_contain_text('权重已保存');page.locator('#weights-close').click()
                    assert session.local_coach.weights.config['failure_expected_loss']==99
                    page.locator('#template-open').click();expect(page.locator('#template-dialog')).to_be_visible()
                    page.locator('#template-field').select_option('failure');page.locator('#template-type').select_option('region')
                    canvas=page.locator('#template-canvas');canvas.scroll_into_view_if_needed();b=canvas.bounding_box()
                    page.mouse.move(b['x']+20,b['y']+20);page.mouse.down();page.mouse.move(b['x']+120,b['y']+65);page.mouse.up();page.locator('#template-save').click()
                    expect(page.locator('#template-error')).to_contain_text('已保存')
                    page.get_by_text('清理采错的模板',exact=True).click();before=len(session.local_coach.fast.bank.data['samples']);page.locator('#template-delete').click()
                    expect(page.locator('#template-delete-select').locator('option')).to_have_count(before-1)
                    page.locator('#template-close').click();expect(page.locator('#template-dialog')).not_to_be_visible()
                    assert session.template_capture is None
                    assert not errors,errors
                    browser.close();print('Chromium passed: automatic five-tab ranking without model, per-slot icons, hash-bound correction, editable weights, local capture/ROI save/delete and capture cleanup')
            finally:
                stop.set();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':main()
