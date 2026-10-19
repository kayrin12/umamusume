import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from umacoach.capture import Gate
from umacoach.guides import load_guide_pack
from umacoach.tracker import Tracker
from umacoach.vision import analyze, validate


def obs(scene='training', turn=20, character='貴婦人', confidence=.96):
    return dict(scene=scene, confidence=confidence, character=character, turn_index=turn,
                date_text='经典级9月前半', evidence=['训练按钮可见'], recommendation='查看速度训练预览',
                reason='其他训练收益尚不可见', missing=['其他四个训练预览'], alternatives=[], guide_refs=[])


class Runs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'runs.json'
        self.tracker = Tracker(self.path)

    def test_full_run_and_abandon_restart(self):
        t = self.tracker
        t.apply(obs('run_start', 0))
        first = t.active.id
        t.apply(obs('training', 10))
        t.apply(obs('abandon_dialog', 10))
        self.assertEqual(t.active.id, first)
        t.apply(obs('abandon_confirmed', None))
        self.assertIsNone(t.active)
        t.apply(obs('run_start', 0))
        self.assertNotEqual(t.active.id, first)
        self.assertIsNone(t.active.last_turn)
        t.apply(obs('training', 71))
        t.apply(obs('result', None))
        self.assertIsNone(t.active)
        self.assertEqual(t.runs[-1].status, 'completed')
        self.assertEqual(len(json.loads(self.path.read_text())), 2)

    def test_same_character_restart_without_observed_abandon_requires_confirmation(self):
        t = self.tracker
        t.new('貴婦人')
        t.apply(obs(turn=50))
        original = t.active.id
        t.apply(obs(turn=2))
        self.assertTrue(t.pending)
        self.assertEqual(t.active.id, original)
        self.assertEqual(t.active.last_turn, 50)
        t.new('貴婦人')
        t.apply(obs(turn=2))
        self.assertFalse(t.pending)
        self.assertNotEqual(t.active.id, original)
        self.assertEqual(t.active.last_turn, 2)

    def test_character_change_is_not_silently_merged(self):
        t = self.tracker
        t.new('貴婦人')
        t.apply(obs(character='黃金巨匠'))
        self.assertTrue(t.pending)
        self.assertEqual(t.active.character, '貴婦人')

    def test_low_confidence_and_title_do_not_end_run(self):
        t = self.tracker
        t.new()
        t.apply(obs('result', confidence=.5))
        self.assertIsNotNone(t.active)
        t.apply(obs('title'))
        self.assertIsNotNone(t.active)

    def test_repeated_opening_does_not_duplicate_runs(self):
        self.tracker.apply(obs('run_start', 0))
        self.tracker.apply(obs('run_start', 0))
        self.assertEqual(len(self.tracker.runs), 1)

    def test_restart_app_requires_reconciliation(self):
        self.tracker.new()
        self.tracker.apply(obs(turn=40))
        reopened = Tracker(self.path)
        self.assertTrue(reopened.pending)
        reopened.apply(obs(turn=41))
        self.assertEqual(reopened.active.last_turn, 40)

    def test_attach_mid_run_does_not_invent_start(self):
        self.tracker.apply(obs())
        self.assertIsNone(self.tracker.active)
        self.assertTrue(self.tracker.pending)

    def test_same_date_event_is_valid(self):
        self.tracker.new()
        self.tracker.apply(obs(turn=20))
        self.tracker.apply(obs('event', turn=20))
        self.assertFalse(self.tracker.pending)


class Frames(unittest.TestCase):
    def test_animation_does_not_block_analysis_forever(self):
        gate = Gate()
        a, b = Image.new('RGB', (100, 100), 'black'), Image.new('RGB', (100, 100), 'white')
        for now in range(20):
            self.assertFalse(gate.ready(a if now % 2 else b, now))
        self.assertTrue(gate.ready(b, 20))

    def test_localized_ui_change_triggers_read(self):
        gate = Gate()
        a = Image.new('RGB', (192, 192), 'black')
        b = a.copy()
        b.paste('white', (0, 0, 12, 12))
        gate.mark(a, 0)
        self.assertFalse(gate.ready(b, 8))
        self.assertTrue(gate.ready(b, 9))

    def test_stability_change_and_cost_throttle(self):
        gate = Gate(interval=8)
        a = Image.new('RGB', (300, 500), 'black')
        b = Image.new('RGB', (300, 500), 'white')
        self.assertFalse(gate.ready(a, 0))
        self.assertTrue(gate.ready(a, 1))
        gate.mark(a, 1)
        self.assertFalse(gate.ready(a, 20))
        self.assertFalse(gate.ready(b, 2))
        self.assertFalse(gate.ready(b, 3))
        self.assertTrue(gate.ready(b, 9))


class Vision(unittest.TestCase):
    def test_reject_invalid_boundary_payload(self):
        for change in ({'scene': 'hacked'}, {'confidence': 2}, {'turn_index': True},
                       {'turn_index': -1}, {'missing': 'unknown'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(obs() | change)

    def test_reject_plaintext_endpoint(self):
        with self.assertRaises(ValueError):
            analyze(Image.new('RGB', (3, 3)), 'http://example.com/v1', 'test', 'model', {}, Path('absent'))

    def test_multimodal_request_and_structured_response(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return json.dumps({'choices': [{'message': {'content': json.dumps(obs())}}]}).encode()

        class Opener:
            def open(self, request, timeout):
                body = json.loads(request.data)
                assert request.full_url == 'https://example.com/v1/chat/completions'
                assert body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
                assert body['response_format']['type'] == 'json_object'
                return Response()

        with patch('urllib.request.build_opener', return_value=Opener()):
            result = analyze(Image.new('RGB', (64, 64)), 'https://example.com/v1', 'test', 'model', {}, Path('absent'))
        self.assertEqual(result['scene'], 'training')
        self.assertEqual(result['character'], '貴婦人')
        with patch('urllib.request.build_opener', return_value=Opener()), patch(__name__+'.obs', return_value=result | {'guide_refs': ['B01']}):
            with self.assertRaisesRegex(ValueError, '未核验'):
                analyze(Image.new('RGB', (64, 64)), 'https://example.com/v1', 'test', 'model', {}, Path('absent'))


class Guides(unittest.TestCase):
    def test_historical_user_source_is_excluded_from_legendary_rating(self):
        path = Path(__file__).resolve().parents[1]/'guides/legendary_tw.md'
        pack, refs = load_guide_pack(path)
        self.assertNotIn('B01', refs)
        self.assertNotIn('"id": "B01"', pack)

    def test_filter_scenario_server_goal_and_unread_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'notes.md'
            path.write_text('核验笔记', 'utf-8')
            base = dict(id='OK',status='verified',server='tw',origin_server='tw',tw_applicability='verified',scenario='legendary',goal='rating',rules=['已确认的规则'])
            entries = [base,
                       base | {'id': 'JP', 'server': 'jp'},
                       base | {'id': 'UAF', 'scenario': 'uaf'},
                       base | {'id': 'RACE', 'goal': 'competition'},
                       base | {'id': 'UNREAD', 'status': 'unread'},
                       base | {'id': 'EMPTY', 'rules': []}]
            path.with_name('sources.json').write_text(json.dumps(entries), 'utf-8')
            pack, refs = load_guide_pack(path)
            self.assertEqual(refs, {'OK'})
            self.assertNotIn('"id": "JP"', pack)
            self.assertNotIn('"id": "RACE"', pack)

    def test_missing_registry_has_no_citable_guide(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack, refs = load_guide_pack(Path(tmp)/'absent.md')
            self.assertEqual(refs, set())
            self.assertIn('暂无已核验攻略', pack)


if __name__ == '__main__':
    unittest.main()
