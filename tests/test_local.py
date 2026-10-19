import json
import queue
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image

from umacoach.local_ocr import (DEFAULT_REGIONS, OcrError, StableReadings, parse_date,
    parse_result, parse_gains, parse_attributes, WindowsOcr)
from umacoach.local_coach import LocalError, date_key, signature
from umacoach.training import ranking, validate_preview
from umacoach.schedule import catalog, alerts
from umacoach.session import Session
from umacoach.tracker import Tracker


def preview(kind='速度',gains=None,failure=0,**extras):
    return dict(kind=kind,gains=gains or [20,0,8,0,0,20],failure=failure,**extras)


def raw(date='古典年5月前半 還有3回合',kind='速度',gains=None,failure=0):
    labels=['速度','耐力','力量','毅力','智力','技能Pt']
    words=[dict(text=t,x=20+i*100,y=10,w=45,h=20) for i,t in enumerate(labels)]
    words += [dict(text='+'+str(n),x=20+i*100,y=40,w=40,h=20) for i,n in enumerate(gains or [20,0,8,0,0,20])]
    return dict(items=[dict(key='header',text=date),dict(key='selected',text=kind),
        dict(key='preview',text=f'{kind}訓練 失敗率{failure}%',words=words)],elapsed_ms=45,language='zh-Hant')


class LocalParsing(unittest.TestCase):
    def test_irregular_boundary_text_is_routed_without_ending_run_locally(self):
        for text,scene in [('養成結束 最終評價','result'),('確定要放棄養成','abandon_dialog'),('粉絲數不足','race')]:
            value=raw();value['items'][-1]['text']=text
            self.assertEqual(parse_result(value)['scene'],scene)
    def test_date_variants_and_remaining_are_not_actual_turn_count(self):
        cases=[('初級年6月前半 還有11回合',10,11),('新馬級6月前半 還有11回合',10,11),('クラシック級9月後半 あと3ターン',41,3),
               ('資深年12月後半',71,None),('3年目1月前半',48,None)]
        for text,index,remaining in cases:
            with self.subTest(text=text):
                value=parse_date(text);self.assertEqual(value['calendar_index'],index);self.assertEqual(value['remaining'],remaining)
                self.assertNotIn('turn_number',value)
        self.assertIsNone(parse_date('9月後半')['calendar_index'])
        self.assertIsNone(parse_date('古典年13月前半')['calendar_index'])

    def test_debut_countdown_can_identify_turn_without_inventing_date(self):
        date=parse_date('出道前 還有11回合')
        self.assertEqual(date['token'],'debut:11')
        self.assertIsNone(date['calendar_index'])

    def test_preview_requires_all_columns_or_calibrated_complete_region(self):
        r=raw()['items'][-1]
        r['words']=[w for w in r['words'] if w['text']!='+0']
        self.assertIn(None,parse_gains(r))
        self.assertEqual(parse_gains(r,True),[20,0,8,0,0,20])
        r['words'].append(dict(text='+7',x=20,y=44,w=30,h=20))
        self.assertIsNone(parse_gains(r,True))

    def test_split_chinese_labels_and_plus_tokens_are_supported(self):
        r=raw()['items'][-1]
        old=r['words'].pop(0)
        r['words'][0:0]=[dict(text='速',x=20,y=10,w=20,h=20),dict(text='度',x=41,y=10,w=20,h=20)]
        first=next(w for w in r['words'] if w['text']=='+20');r['words'].remove(first)
        r['words'] += [dict(text='+',x=20,y=40,w=10,h=20),dict(text='20',x=31,y=40,w=25,h=20)]
        self.assertEqual(parse_gains(r,True),[20,0,8,0,0,20])

    def test_stats_are_not_confused_with_training_increases(self):
        r=raw()['items'][-1]
        r['words']=[w for w in r['words'] if w['y']==10 and w['text']!='技能Pt']
        r['words'] += [dict(text=str(100+i),x=20+i*100,y=40,w=40,h=20) for i in range(5)]
        self.assertEqual(parse_attributes(r)['speed'],100)
        r['words'].append(dict(text='200',x=20,y=80,w=40,h=20))
        self.assertIsNone(parse_attributes(r))

    def test_current_fans_and_required_fans_use_separate_regions(self):
        r=raw();r['items'] += [dict(key='fans',text='粉絲數12,500人'),dict(key='goal',text='粉絲數至少15000人')]
        p=parse_result(r,True)
        self.assertEqual(p['fans'],12500);self.assertEqual(p['goal_required'],15000)
        self.assertEqual(p['kind'],'速度');self.assertEqual(p['failure'],0)
        r['items'][-1]['text']='粉絲數至少6,00O人'
        self.assertIsNone(parse_result(r,True)['goal_required'])

    def test_ambiguous_training_buttons_are_not_selected_training(self):
        r=raw();r['items']=[x for x in r['items'] if x['key']!='selected']
        r['items'][-1]['text']='速度訓練 耐力訓練 力量訓練'
        self.assertIsNone(parse_result(r)['kind'])

    def test_stability_is_per_field_not_a_fabricated_confidence(self):
        gate=StableReadings()
        self.assertEqual(gate.confirm(dict(fans=3000,date=10)),{})
        self.assertEqual(gate.confirm(dict(fans=3000,date=11)),dict(fans=3000))
        self.assertEqual(gate.confirm(dict(fans=None,date=11)),dict(date=11))


class TrainingRules(unittest.TestCase):
    def test_ranking_is_instant_deterministic_and_scope_is_explicit(self):
        state=dict(guide_gauges={})
        cache={'速度':dict(at=100,preview=validate_preview(preview())),
               '耐力':dict(at=100,preview=validate_preview(preview('耐力',[0,30,10,0,0,40])))}
        result=ranking(cache,state,40,now=100)
        self.assertEqual(result['rows'][0]['kind'],'耐力')
        self.assertIn('仅比较已见',result['scope'])
        self.assertIn('耐力',result['recommendation'])
        self.assertEqual(result,ranking(cache,state,40,now=100))

    def test_high_risk_training_does_not_win_just_by_large_gains(self):
        cache={'速度':dict(at=100,preview=validate_preview(preview(gains=[100]*6,failure=80))),
               '智力':dict(at=100,preview=validate_preview(preview('智力',[0,0,0,0,10,10])))}
        result=ranking(cache,dict(guide_gauges={}),40,now=100)
        self.assertIn('优先点「智力」',result['recommendation'])

    def test_expired_previews_do_not_appear_as_current_options(self):
        result=ranking({'速度':dict(at=100,preview=validate_preview(preview()))},dict(guide_gauges={}),40,now=221)
        self.assertEqual(result['rows'],[])

    def test_missing_gains_are_not_silently_zero(self):
        for bad in [preview(gains=[20,None,0,0,0,10]),preview(failure=None),preview(gains=[1]*5),preview(friends=7)]:
            with self.assertRaises(ValueError):validate_preview(bad)

    def test_early_bond_and_late_skill_weights_differ(self):
        a=validate_preview(preview(gains=[10,0,0,0,0,0],unbonded=3))
        b=validate_preview(preview('耐力',[0,0,0,0,0,15],unbonded=0))
        cache={'速度':dict(at=0,preview=a),'耐力':dict(at=0,preview=b)}
        self.assertEqual(ranking(cache,dict(guide_gauges={}),12,now=0)['rows'][0]['kind'],'速度')
        self.assertEqual(ranking(cache,dict(guide_gauges={}),40,now=0)['rows'][0]['kind'],'耐力')


class LocalIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Session(Path(self.tmp.name));self.s.tracker.new();self.local=self.s.local_coach

    def apply(self,value):
        p=parse_result(value,True);self.local._apply(p);self.local._apply(p)

    def wait(self):
        deadline=time.monotonic()+3
        while self.local.busy and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.local.busy)

    def test_automatic_preview_cache_cross_date_clear_and_unknown_suppression(self):
        self.apply(raw());self.assertIn('速度',self.s.tracker.active.local['training'])
        self.apply(raw(kind='耐力'));self.assertEqual(len(self.local.snapshot()['training']['rows']),2)
        self.apply(raw(date='古典年5月後半 還有2回合',kind='智力'))
        self.assertEqual(list(self.s.tracker.active.local['training']),['智力'])
        self.local._apply(parse_result(raw(date='看不清日期'),True))
        self.assertIsNone(self.local.snapshot()['training'])

    def test_debut_turn_cache_and_countdown_rollback(self):
        self.apply(raw(date='出道前 還有10回合'))
        self.assertIsNotNone(self.local.snapshot()['training'])
        self.apply(raw(date='出道前 還有9回合'))
        self.assertFalse(self.s.tracker.pending)
        self.apply(raw(date='出道前 還有11回合'))
        self.assertTrue(self.s.tracker.pending)
        self.assertIsNone(self.local.snapshot()['training'])

    def test_manual_profile_locks_character_and_survives_restart(self):
        profile=next(p for p in catalog()['profiles'] if p['name']=='貴婦人')
        self.local.manual(self.s.tracker.active.id,dict(profile_id=profile['id']))
        run=self.s.tracker.active
        observation=dict(scene='training',confidence=.99,character='黃金巨匠',turn_index=20,date_text='',state_update=None)
        self.s.tracker.apply(observation)
        self.assertEqual(run.character,'貴婦人');self.assertFalse(self.s.tracker.pending)
        reopened=Tracker(self.s.tracker.path)
        self.assertTrue(reopened.active.character_locked)
        self.assertEqual(reopened.active.local['profile_id'],profile['id'])
        self.s.boundary('resume')
        self.assertEqual(self.s.tracker.active.character,'貴婦人')
        self.assertTrue(self.s.tracker.active.character_locked)

    def test_manual_date_anchor_fans_and_preview_are_immediate(self):
        run=self.s.tracker.active
        self.local.manual(run.id,dict(character='貴婦人',calendar_index=32,remaining=3,turn_number=23,fans=1000))
        self.local.manual(run.id,dict(preview=preview(),preview_date=32))
        snap=self.local.snapshot();self.assertEqual(snap['turn_number'],23)
        self.assertEqual(snap['training']['rows'][0]['kind'],'速度')
        with self.assertRaises(LocalError):self.local.manual(run.id,dict(preview=preview(),preview_date=31))

    def test_resource_change_invalidates_cached_comparison(self):
        self.apply(raw());self.assertTrue(self.local.snapshot()['training']['rows'])
        self.s.tracker.active.state['mood']='好調'
        self.assertEqual(self.local.snapshot()['training']['rows'],[])

    def test_low_quality_or_single_frame_does_not_overwrite_confirmed_fans(self):
        self.s.tracker.active.state['fans']=5000
        r=raw();r['items'].append(dict(key='fans',text='粉絲數3000人'))
        self.local._apply(parse_result(r,True))
        self.assertEqual(self.s.tracker.active.state['fans'],5000)
        self.local._apply(parse_result(r,True))
        self.assertTrue(self.s.tracker.pending)
        self.assertEqual(self.s.tracker.active.state['fans'],5000)

    def test_ocr_failure_has_cooldown_and_no_automatic_model_call(self):
        self.local.settings['enabled']=True;self.s.hwnd=123
        with patch.object(self.local.ocr,'recognize',side_effect=OcrError('缺少识别语言')),patch('umacoach.session.codex_backend.analyze_codex') as ai:
            with self.s.lock:self.local.submit(Image.new('RGB',(100,100)))
            self.wait()
        self.assertIn('缺少识别语言',self.local.message)
        self.assertGreater(self.local.error_until,time.monotonic());ai.assert_not_called()

    def test_ocr_does_not_wait_for_busy_model(self):
        self.local.settings['enabled']=True;self.s.busy=True;self.s.hwnd=123
        with patch('umacoach.session.capture',return_value=Image.new('RGB',(100,100))),patch.object(self.local.ocr,'recognize',return_value=raw()) as ocr:
            self.s.read_now();self.wait()
        ocr.assert_called_once()

    def test_old_ocr_result_cannot_modify_new_run(self):
        entered,release=threading.Event(),threading.Event()
        def delayed(*args):entered.set();release.wait(2);return raw()
        with patch.object(self.local.ocr,'recognize',side_effect=delayed):
            with self.s.lock:self.local.submit(Image.new('RGB',(100,100)),True)
            entered.wait(1);self.s.boundary('new');release.set();self.wait()
        self.assertEqual(self.s.tracker.active.local,{})

    def test_samples_pending_runs_and_manual_invalid_values_are_rejected(self):
        run=self.s.tracker.active
        for values in [dict(calendar_index=72),dict(fans=-1),dict(profile_id='bad'),dict(character=''),dict(preview=preview())]:
            with self.assertRaises(LocalError):self.local.manual(run.id,values)
        self.s.sample=True
        with self.assertRaises(LocalError):self.local.manual(run.id,dict(fans=100))

    def test_settings_validation_and_no_shared_defaults(self):
        original=dict(self.local.settings)
        with self.assertRaises(LocalError):self.local.configure(dict(regions=dict(header=[0,0,2,1])))
        self.local.configure(dict(regions=dict(header=[0,0,.5,.2]),preview_complete=True))
        self.assertEqual(DEFAULT_REGIONS['header'],[0,0,1,.38])
        self.assertTrue(json.loads(self.local.path.read_text())['preview_complete'])


class FanSchedule(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Session(Path(self.tmp.name));self.s.tracker.new('貴婦人');self.run=self.s.tracker.active
        self.run.local=dict(date=dict(calendar_index=30),goals=[dict(name='报名门槛',deadline=33,required_fans=6000,done=False)])

    def test_catalog_includes_users_characters_with_explicit_missing_thresholds(self):
        data=catalog()
        for name in ['貴婦人','黃金巨匠']:
            self.assertTrue(any(p['name']==name and p['goals'] for p in data['profiles']))
        self.assertEqual(len(data['profiles']),204)
        self.assertEqual(data['source']['tw_applicability'],'unverified')
        self.assertTrue(all(g['required_fans'] is None for p in data['profiles'] for g in p['goals']))

    def test_fan_deficit_takes_precedence_and_unknown_never_means_safe(self):
        self.run.state['fans']=1000
        result=alerts(self.run);self.assertTrue(result['urgent'])
        self.assertTrue(any('仍差5000' in m for m in result['messages']))
        self.run.state['fans']=None
        self.assertTrue(alerts(self.run)['urgent'])
        self.assertFalse(any('门槛满足' in m for m in alerts(self.run)['messages']))

    def test_met_threshold_does_not_claim_all_entry_conditions_satisfied(self):
        self.run.state['fans']=7000
        self.assertFalse(alerts(self.run)['urgent'])
        self.assertTrue(any('其他出赛条件仍须检查' in m for m in alerts(self.run)['messages']))

    def test_overdue_unconfirmed_goal_is_not_assumed_completed(self):
        self.run.local['date']['calendar_index']=34
        self.assertTrue(alerts(self.run)['urgent'])
        self.run.local['goals'][0]['done']=True
        self.assertFalse(alerts(self.run)['urgent'])

    def test_unverified_reference_does_not_create_mandatory_goals(self):
        profile=next(p for p in catalog()['profiles'] if p['name']=='貴婦人')
        self.run.local['profile_id']=profile['id'];self.run.local['goals']=[]
        result=alerts(self.run)
        self.assertTrue(result['reference_goals']);self.assertFalse(result['urgent'])
        self.assertTrue(any('没有已确认' in m for m in result['messages']))

    def test_candidates_are_reference_only_and_use_base_affinities(self):
        profile=next(p for p in catalog()['profiles'] if p['name']=='貴婦人')
        self.run.local['profile_id']=profile['id']
        result=alerts(self.run)
        self.assertTrue(result['candidates'])
        self.assertTrue(all(30<=r['calendar_index']<=33 for r in result['candidates']))
        self.assertTrue(all(r['entry_fans'] is None for r in result['candidates']))


class OcrProcessProtocol(unittest.TestCase):
    def test_persistent_local_protocol_and_temporary_crop_cleanup(self):
        class Output:
            def __init__(self):self.messages=queue.Queue();self.messages.put(json.dumps(dict(ready=True,language='zh-Hant')))
            def __iter__(self):return self
            def __next__(self):
                value=self.messages.get(timeout=2)
                if value is None:raise StopIteration
                return value+'\n'
        class Input:
            def __init__(self,output):self.output=output;self.buffer='';self.paths=[]
            def write(self,text):self.buffer+=text
            def flush(self):
                job=json.loads(self.buffer);self.buffer=''
                for item in job['items']:
                    self.paths.append(item['path']);assert Path(item['path']).exists()
                self.output.messages.put(json.dumps(dict(ok=True,items=[])))
        class Process:
            def __init__(self):self.stdout=Output();self.stdin=Input(self.stdout);self.stopped=False
            def poll(self):return 0 if self.stopped else None
            def terminate(self):self.stopped=True;self.stdout.messages.put(None)
            def kill(self):self.terminate()
            def wait(self,timeout):return 0
        process=Process()
        with tempfile.TemporaryDirectory() as tmp:
            engine=WindowsOcr(Path(tmp)/'ocr-tmp')
            with patch('umacoach.local_ocr.sys.platform','win32'),patch('umacoach.local_ocr.subprocess.CREATE_NO_WINDOW',0,create=True),patch('umacoach.local_ocr.subprocess.Popen',return_value=process) as start:
                for _ in range(2):engine.recognize(Image.new('RGB',(200,300)),dict(header=[0,0,1,.3]))
                self.assertEqual(start.call_count,1)
                command=start.call_args.args[0]
                self.assertNotIn('-ExecutionPolicy',command)
                self.assertIn('-Command',command)
                self.assertTrue(all(not Path(path).exists() for path in process.stdin.paths))
                self.assertEqual(list((Path(tmp)/'ocr-tmp').iterdir()),[])
                engine.close()


if __name__=='__main__':unittest.main()
