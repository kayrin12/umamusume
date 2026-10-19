"""Regression tests for persistent evidence, uncertain boundaries and editing races."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from io import BytesIO
import subprocess

from PIL import Image

from umacoach.codex_backend import schema
from umacoach.guides import load_guide_pack
from umacoach.run_state import (StateError, VALUES, context_state, empty_state,
                               merge_state, validate_update)
from umacoach.session import Session
from umacoach.tracker import Tracker
from umacoach.vision import PROMPT, validate
from umacoach.vision import analyze
from umacoach import codex_backend as cb

ROOT = Path(__file__).resolve().parents[1]


def env(value, confidence=.98):
    return dict(value=value, confidence=confidence, evidence='当前画面明确显示')


def insight(name, color='pink', stars=2):
    return dict(name=name, color=color, stars=stars)


def obs(scene='training', confidence=.99, turn=20, **update):
    return dict(scene=scene, confidence=confidence, character='貴婦人', turn_index=turn,
        date_text='古典年5月前半', evidence=['训练按钮'], recommendation='查看训练',
        reason='测试', missing=[], alternatives=[], guide_refs=[], state_update=update)


class GuideApplicability(unittest.TestCase):
    def test_delivered_rules_keep_assumptions_and_verbatim_qualifiers(self):
        pack, ids = load_guide_pack(ROOT/'guides/legendary_tw.md')
        self.assertEqual(ids, {'J01', 'J02', 'W01'})
        self.assertIn('跨服推定、仅供参考', pack)
        self.assertIn('【有争议】', pack)
        self.assertIn('【作者经验】', pack)
        self.assertIn('"origin_server": "jp"', pack)
        registry=json.loads((ROOT/'guides/sources.json').read_text('utf-8'))
        for source in registry:
            self.assertTrue(source['url'].startswith('https://'))
            if source['id'] in ids:
                for rule in source['rules']:
                    self.assertIn(rule, pack)
        self.assertIn('跨服推定', PROMPT)
        self.assertIn('不得称为最优', PROMPT)

    def test_applicability_is_explicit_and_unverified_or_legacy_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'notes.md'
            path.write_text('', 'utf-8')
            base = dict(id='CONFIRMED', status='verified', server='tw', origin_server='tw',
                tw_applicability='verified', scenario='legendary', goal='rating', rules=['规则'])
            assumed = base | dict(id='ASSUMED', origin_server='jp', tw_applicability='assumed')
            legacy = base.copy();legacy.pop('tw_applicability');legacy['id']='LEGACY'
            entries=[base, assumed, base|dict(id='UNKNOWN',tw_applicability='unverified'),
                     base|dict(id='TYPO',tw_applicability='verifed'),
                     base|dict(id='ORIGIN',origin_server=None), legacy]
            path.with_name('sources.json').write_text(json.dumps(entries), 'utf-8')
            pack, refs = load_guide_pack(path)
            self.assertEqual(refs, {'CONFIRMED','ASSUMED'})
            self.assertIn('已核验繁中服适用性', pack)

    def test_api_accepts_assumed_source_but_rejects_unknown_or_rule_ids(self):
        class Response(BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        for refs in [['J01'],['B01'],['J01-1'],['UNLISTED']]:
            value=obs();value['guide_refs']=refs
            response=Response(json.dumps(dict(choices=[dict(message=dict(content=json.dumps(value)))] )).encode())
            with self.subTest(refs=refs),patch('urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=response
                if refs==['J01']:
                    self.assertEqual(analyze(Image.new('RGB',(20,20)),'https://example.com/v1','test','test',{},ROOT/'guides/legendary_tw.md')['guide_refs'],refs)
                else:
                    with self.assertRaises(ValueError):
                        analyze(Image.new('RGB',(20,20)),'https://example.com/v1','test','test',{},ROOT/'guides/legendary_tw.md')

    def test_codex_receives_state_and_rejects_out_of_pack_ids(self):
        for refs in [['J02'],['B01'],['J01-1']]:
            value=obs();value['guide_refs']=refs
            with self.subTest(refs=refs),tempfile.TemporaryDirectory() as tmp:
                def run(command,**kwargs):
                    self.assertIn('很早拿到的心得',kwargs['input'])
                    self.assertIn('跨服推定、仅供参考',kwargs['input'])
                    Path(command[command.index('-o')+1]).write_text(json.dumps(value),'utf-8')
                    return subprocess.CompletedProcess(command,0,'','')
                with patch.object(cb,'ROOT',Path(tmp)),patch.object(cb,'binary',return_value='/fake/codex'),patch.object(cb,'login_status',return_value='ready'),patch.object(cb.subprocess,'run',side_effect=run):
                    args=(Image.new('RGB',(20,20)),dict(run_state=dict(insights=[insight('很早拿到的心得')])),ROOT/'guides/legendary_tw.md')
                    if refs==['J02']:
                        self.assertEqual(cb.analyze_codex(*args)['guide_refs'],refs)
                    else:
                        with self.assertRaises(cb.CodexError):cb.analyze_codex(*args)


class StateMerge(unittest.TestCase):
    def merge(self, update, state=None, **kwargs):
        return merge_state(state or empty_state(), update, **kwargs)[0]

    def test_all_requested_state_fields_survive_partial_updates(self):
        update=dict(mentor=env('俳成功'), insights=env([insight('谈话术')]),
            guide_gauges=env(dict(blue=1,green=3,pink=8)), turns_to_insight=env(2),
            outings_used=env(2), friend_levels=env([dict(name='支援A',level='MAX')]),
            energy=env(dict(current=45,maximum=110)), mood=env('好調'),
            races=env([dict(name='日本德比',date_text='古典年5月后半',place=1,grade='G1')]),
            race_totals=env(dict(starts=7,wins=5)),
            dream_fest=env([dict(stage='Stella',date_text='初级年12月后半',place=1)]))
        first = self.merge(update, turn=32, date_text='古典年5月前半')
        second = self.merge(dict(energy=env(dict(current=30,maximum=None))), first, turn=33)
        self.assertEqual(second['energy'], dict(current=30,maximum=110))
        self.assertEqual(second['guide_gauges'], first['guide_gauges'])
        self.assertEqual(second['mentor'], '俳成功')
        self.assertEqual(second['race_totals']['wins'], 5)
        self.assertEqual(second['evidence']['energy.maximum']['turn_index'], 32)
        self.assertEqual(second['evidence']['energy.current']['turn_index'], 33)
        self.assertEqual(context_state(second)['observed_race_counts'], dict(starts=1,wins=1))
        self.assertEqual(second['evidence']['mentor']['source'], 'model')

    def test_low_confidence_null_and_omission_never_erase_confirmed_values(self):
        first=self.merge(dict(outings_used=2,mentor='俳成功'),manual=True)
        second=self.merge(dict(outings_used=env(0,.6),mentor=None,energy=env(None)),first)
        self.assertEqual(second,first)
        validate(obs())
        legacy=obs();legacy.pop('state_update');validate(legacy)

    def test_invalid_ranges_types_and_empty_evidence_are_rejected(self):
        for key,value in [('outings_used',6),('turns_to_insight',7),('mentor','海塞克'),
                          ('outings_used',True),('mood','猜测'),('insights',[insight('A',stars=4)])]:
            with self.subTest(key=key,value=value),self.assertRaises(StateError):
                validate_update({key:env(value)})
        for bad in [env(2,float('nan')),env(2,True),{'value':2,'confidence':.98,'evidence':''}]:
            with self.assertRaises(StateError):validate_update(dict(outings_used=bad))

    def test_conflicting_energy_and_totals_are_atomic(self):
        first=self.merge(dict(energy=dict(current=50,maximum=100)),manual=True)
        saved=deepcopy(first)
        for update in [dict(energy=env(dict(current=101,maximum=None))),
                       dict(race_totals=env(dict(starts=3,wins=4)))]:
            with self.assertRaises(StateError):self.merge(update,first)
            self.assertEqual(first,saved)

    def test_partial_insights_do_not_remove_offscreen_entries(self):
        first=self.merge(dict(insights=env([insight('A'),insight('B')])))
        second=self.merge(dict(insights=env([insight('A',stars=3)])),first)
        self.assertEqual([i['name'] for i in second['insights']],['A','B'])
        self.assertEqual(second['insights'][0]['stars'],3)
        self.assertEqual(second['replacements'],[])

    def test_explicit_replacements_at_capacity_are_recorded_and_deduplicated(self):
        first=self.merge(dict(insights=env([insight(str(i)) for i in range(10)])))
        update=dict(insight_replacements=env([dict(old_name='0',new_insight=insight('NEW'))]),
                    insights=env([insight('NEW')]))
        second=self.merge(update,first,turn=42)
        third=self.merge(update,second,turn=42)
        self.assertEqual(len(second['insights']),10)
        self.assertEqual(second['replacements'][0]['removed'],[insight('0')])
        self.assertEqual(second['replacements'][0]['added'],[insight('NEW')])
        self.assertEqual(len(third['replacements']),1)
        with self.assertRaises(StateError):self.merge(dict(insights=env([insight('ELEVENTH')])),second)
        with self.assertRaises(StateError):self.merge(dict(insight_replacements=env([dict(old_name='not-recorded',new_insight=insight('X'))])),first)

    def test_manual_full_list_corrects_and_records_replacement(self):
        first=self.merge(dict(insights=env([insight('A'),insight('B')])))
        second=self.merge(dict(insights=[insight('B'),insight('C')],outings_used=None),first,manual=True)
        self.assertEqual(second['replacements'][0]['source'],'manual')
        self.assertEqual(second['insights'],[insight('B'),insight('C')])
        self.assertIsNone(second['outings_used'])

    def test_duplicate_names_are_rejected_not_silently_collapsed(self):
        with self.assertRaises(StateError):self.merge(dict(insights=env([insight('A'),insight('A')])))
        with self.assertRaises(StateError):self.merge(dict(friend_levels=[dict(name='A',level=1)]*2),manual=True)

    def test_repeat_race_and_fest_frames_do_not_inflate_counts(self):
        race=dict(name='比赛A',date_text='古典年5月后半',place=1,grade='G1')
        fest=dict(stage='Stella',date_text='初级年12月后半',place=1)
        first=self.merge(dict(races=env([race]),dream_fest=env([fest])))
        second=self.merge(dict(races=env([race|dict(place=None)]),dream_fest=env([fest])),first)
        self.assertEqual(context_state(second)['observed_race_counts'],dict(starts=1,wins=1))
        self.assertEqual(len(second['dream_fest']),1)
        self.assertIsNone(second['race_totals']['starts'])

    def test_omitted_zero_and_unknown_are_distinct(self):
        state=self.merge(dict(outings_used=env(0),turns_to_insight=env(0)))
        self.assertEqual(state['outings_used'],0)
        self.assertEqual(state['turns_to_insight'],0)
        self.assertIsNone(state['energy']['current'])

    def test_codex_schema_marks_unseen_patch_nullable_and_objects_strict(self):
        definition=schema()
        self.assertIn('state_update',definition['properties'])
        def walk(node):
            if isinstance(node,dict):
                if node.get('type')=='object':
                    self.assertFalse(node['additionalProperties'])
                    self.assertEqual(set(node['required']),set(node['properties']))
                for v in node.values():walk(v)
            elif isinstance(node,list):
                for v in node:walk(v)
        walk(definition)


class StatePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'runs.json';self.t=Tracker(self.path)

    def test_state_survives_restart_and_new_run_has_no_old_resources(self):
        self.t.new('貴婦人')
        self.t.apply(obs(outings_used=env(4),insights=env([insight('A')])))
        reopened=Tracker(self.path)
        self.assertTrue(reopened.pending)
        self.assertEqual(reopened.active.state['outings_used'],4)
        reopened.new('貴婦人')
        self.assertIsNone(reopened.active.state['outings_used'])
        self.assertIsNone(reopened.active.state['insights'])
        self.assertEqual(reopened.runs[0].state['outings_used'],4)

    def test_legacy_runs_without_state_migrate_without_guessing(self):
        self.t.new();payload=json.loads(self.path.read_text());payload[0].pop('state');payload[0].pop('state_revision')
        self.path.write_text(json.dumps(payload),'utf-8')
        reopened=Tracker(self.path)
        self.assertEqual(reopened.active.state,empty_state())
        self.assertEqual(reopened.active.state_revision,0)

    def test_uncertain_boundary_and_low_scene_confidence_do_not_merge(self):
        self.t.new('貴婦人')
        self.t.apply(obs(turn=50,outings_used=env(3)))
        self.t.apply(obs(turn=51,confidence=.86,outings_used=env(0)))
        self.assertEqual(self.t.active.state['outings_used'],3)
        self.t.apply(obs(turn=2,outings_used=env(0)))
        self.assertTrue(self.t.pending)
        self.assertEqual(self.t.active.state['outings_used'],3)

    def test_final_result_saves_state_on_finished_run(self):
        self.t.new('貴婦人')
        self.t.apply(obs('result',race_totals=env(dict(starts=12,wins=11))))
        self.assertIsNone(self.t.active)
        self.assertEqual(Tracker(self.path).runs[-1].state['race_totals']['wins'],11)


class StateSession(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Session(Path(self.tmp.name));self.s.provider='api';self.s.tracker.new('貴婦人')

    def wait(self):
        deadline=time.monotonic()+3
        while self.s.busy and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.s.busy)

    def test_manual_correction_persists_and_rejects_old_run_or_revision(self):
        s=self.s;run=s.tracker.active
        s.correct_state(run.id,0,dict(mentor='俳成功',outings_used=2))
        self.assertEqual(s.snapshot()['run_state']['outings_used'],2)
        self.assertEqual(Tracker(s.tracker.path).active.state['mentor'],'俳成功')
        with self.assertRaises(StateError):s.correct_state(run.id,0,dict(outings_used=1))
        with self.assertRaises(StateError):s.correct_state('wrong-run',1,dict(outings_used=1))
        s.tracker.pending=True
        with self.assertRaises(StateError):s.correct_state(run.id,1,dict(outings_used=1))
        s.tracker.pending=False;s.sample=True
        with self.assertRaises(StateError):s.correct_state(run.id,1,dict(outings_used=1))

    def test_analysis_context_contains_persistent_state_beyond_four_observations(self):
        s=self.s;s.tracker.active.state['outings_used']=3
        s.tracker.active.state['insights']=[insight('很早拿到的心得')]
        with patch('umacoach.session.analyze',return_value=obs()) as analyze:
            with s.lock:s.submit(Image.new('RGB',(20,20)),False)
            self.wait()
        context=analyze.call_args.args[4]
        self.assertEqual(context['run_state']['outings_used'],3)
        self.assertEqual(context['run_state']['insights'][0]['name'],'很早拿到的心得')
        s.tracker.active.state['outings_used']=4
        self.assertEqual(context['run_state']['outings_used'],3) # immutable snapshot

    def test_manual_correction_invalidates_pending_model_result(self):
        s=self.s;entered,release=threading.Event(),threading.Event()
        def delayed(*args):entered.set();release.wait(2);return obs(outings_used=env(4))
        with patch('umacoach.session.analyze',side_effect=delayed):
            with s.lock:s.submit(Image.new('RGB',(20,20)),False)
            self.assertTrue(entered.wait(1))
            s.correct_state(s.tracker.active.id,0,dict(outings_used=2))
            release.set();self.wait()
        self.assertEqual(s.tracker.active.state['outings_used'],2)
        self.assertIsNone(s.result)

    def test_pending_run_omits_old_state_from_context(self):
        self.s.tracker.pending=True
        with patch('umacoach.session.analyze',return_value=obs()) as analyze:
            with self.s.lock:self.s.submit(Image.new('RGB',(20,20)),False)
            self.wait()
        self.assertIsNone(analyze.call_args.args[4]['run_state'])

    def test_sample_and_old_run_responses_cannot_modify_actual_state(self):
        s=self.s
        with patch('umacoach.session.analyze',return_value=obs(outings_used=env(5))):
            with s.lock:s.submit(Image.new('RGB',(20,20)),True)
            self.wait()
        self.assertIsNone(s.tracker.active.state['outings_used'])
        entered,release=threading.Event(),threading.Event()
        def delayed(*args):entered.set();release.wait(2);return obs(outings_used=env(5))
        with patch('umacoach.session.analyze',side_effect=delayed):
            with s.lock:s.submit(Image.new('RGB',(20,20)),False)
            entered.wait(1);s.boundary('new');release.set();self.wait()
        self.assertIsNone(s.tracker.active.state['outings_used'])


if __name__=='__main__':unittest.main()
