import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from PIL import Image

from umacoach.session import Session
from umacoach.capture import CaptureError
from umacoach.webapp import Server, check_local_server


def observation():
    return dict(scene='run_start', confidence=.99, character='貴婦人', date_text='', turn_index=None,
                evidence=['模拟开局'], recommendation='测试建议', reason='测试', missing=[], alternatives=[], guide_refs=[])


class Portable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Session(Path(self.tmp.name))
        self.session.key = ''
        self.session.provider = 'api'

    def complete(self):
        deadline = time.monotonic()+3
        while self.session.snapshot()['busy'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.session.snapshot()['busy'])

    def test_preview_works_without_python_installer_or_api_key(self):
        s = self.session
        with patch('umacoach.session.windows', return_value=[(123, '赛马娘')]), patch('umacoach.session.capture', return_value=Image.new('RGB', (300, 500), 'green')):
            s.configure(dict(hwnd=123))
            s.toggle(True)
            s.tick()
        state = s.snapshot()
        self.assertTrue(state['auto'])
        self.assertTrue(state['preview'])
        self.assertFalse(state['has_key'])
        self.assertIsNone(state['result'])
        with Image.open(BytesIO(base64.b64decode(state['preview']))) as pic:
            self.assertEqual(pic.format, 'JPEG')

    def test_api_key_is_never_in_state_or_saved_configuration(self):
        s = self.session
        s.configure({'key': 'private-test-token'})
        self.assertTrue(s.snapshot()['has_key'])
        self.assertNotIn('private-test-token', json.dumps(s.snapshot()))
        self.assertFalse(list(Path(self.tmp.name).glob('*')))

    def test_old_run_worker_response_cannot_enter_new_run(self):
        s = self.session
        entered, release = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            release.wait(2)
            return observation()
        with patch('umacoach.session.analyze', side_effect=delayed):
            with s.lock:
                s.submit(Image.new('RGB', (20, 20)), False)
            self.assertTrue(entered.wait(1))
            s.boundary('new')
            current = s.tracker.active.id
            release.set()
            self.complete()
        self.assertEqual(s.tracker.active.id, current)
        self.assertIsNone(s.snapshot()['result'])

    def test_imported_sample_never_starts_a_real_run(self):
        image = BytesIO()
        Image.new('RGB', (30, 40)).save(image, format='PNG')
        self.session.key = 'test'
        with patch('umacoach.session.analyze', return_value=observation()):
            self.session.import_image(base64.b64encode(image.getvalue()).decode())
            self.complete()
        self.assertTrue(self.session.snapshot()['sample'])
        self.assertEqual(self.session.snapshot()['result']['recommendation'], '测试建议')
        self.assertIsNone(self.session.tracker.active)
        self.assertFalse((Path(self.tmp.name)/'runs.json').exists())

    def test_frame_changed_during_inference_drops_obsolete_recommendation(self):
        s = self.session
        s.auto = True
        s.set_frame(Image.new('RGB', (20, 20), 'white'))
        with patch('umacoach.session.analyze', return_value=observation()):
            with s.lock:
                s.submit(Image.new('RGB', (20, 20), 'black'), False)
            self.complete()
        self.assertIsNone(s.snapshot()['result'])
        self.assertIsNone(s.tracker.active)

    def test_api_failure_pauses_automatic_requests(self):
        s = self.session
        s.auto = True
        with patch('umacoach.session.analyze', side_effect=RuntimeError('private-test-token')):
            with s.lock:
                s.submit(Image.new('RGB', (20, 20)), False)
            self.complete()
        self.assertFalse(s.snapshot()['auto'])
        self.assertNotIn('private-test-token', json.dumps(s.snapshot()))

    def test_uncertain_restart_hides_suggestion(self):
        s = self.session
        s.tracker.new('貴婦人')
        s.tracker.active.last_turn = 50
        value = observation() | {'scene': 'training', 'turn_index': 2}
        with patch('umacoach.session.analyze', return_value=value):
            with s.lock:
                s.submit(Image.new('RGB', (20, 20)), False)
            self.complete()
        self.assertTrue(s.snapshot()['pending'])
        self.assertIsNone(s.snapshot()['result'])

    def test_foreground_picker_works_without_window_list(self):
        s = self.session
        with patch('umacoach.session.foreground_window', return_value=(2**40, '无标题游戏')), patch('umacoach.session.capture', return_value=Image.new('RGB', (300, 500))), patch('umacoach.session.windows', side_effect=CaptureError('列表不可用')):
            s.pick_window(delay=0)
            deadline = time.monotonic()+2
            while s.snapshot()['picking'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(s.snapshot()['hwnd'], 2**40)
            self.assertTrue(s.snapshot()['preview'])
            s.configure({'hwnd': str(2**40), 'key': 'private-test'})
            self.assertTrue(s.snapshot()['has_key'])

    def test_region_selection_avoids_enum_and_never_analyzes_desktop(self):
        s = self.session
        s.key = 'test'
        desktop = dict(image='local-desktop-preview', preview_size=[400, 300], bounds=[-800, 0, 1600, 1200])
        with patch('umacoach.session.desktop_snapshot', return_value=desktop), patch('umacoach.session.analyze') as analyze, patch('umacoach.session.capture_region', return_value=Image.new('RGB', (400, 800))) as grab, patch('umacoach.session.windows', side_effect=CaptureError('列表不可用')):
            selection = s.prepare_region()
            self.assertIsNone(s.frame)
            s.select_region(selection['selection_id'], [10, 20, 100, 200])
            self.assertEqual(s.region, (-760, 80, -360, 880))
            grab.assert_called_with((-760, 80, -360, 880))
            analyze.assert_not_called()
            self.assertTrue(s.preview)
            self.assertNotIn('local-desktop-preview', s.preview)
            s.configure({'hwnd': 'region'})
            self.assertEqual(s.region, (-760, 80, -360, 880))

    def test_region_requires_fresh_selection_token(self):
        with self.assertRaises(CaptureError):
            self.session.select_region('not-issued', [0, 0, 100, 100])

    def test_capture_error_is_retained_for_diagnostics_without_key(self):
        s = self.session
        s.hwnd = 123
        s.key = 'private-test'
        s.auto = True
        with patch('umacoach.session.capture', side_effect=CaptureError('窗口已最小化')):
            s.tick()
        self.assertEqual(s.diagnostics()['last_capture_error'], '窗口已最小化')
        self.assertNotIn('private-test', json.dumps(s.diagnostics()))
        self.assertFalse(s.auto)


class HTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = Server(('127.0.0.1', 0), Session(Path(self.tmp.name)))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, data=None, headers=None):
        h = {'X-Umacoach-Token': self.server.token}
        if data is not None:
            h['Content-Type'] = 'application/json'
        if headers:
            h.update(headers)
        req = urllib.request.Request(self.server.origin+path,
            data=json.dumps(data).encode() if data is not None else None, headers=h)
        # Local requests must not depend on any machine-level Internet proxy.
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3)

    def test_panel_serves_chinese_and_api_round_trip(self):
        with self.request('/') as r:
            html = r.read().decode()
            self.assertIn('赛马娘养成助手', html)
            self.assertNotIn('__TOKEN__', html)
            self.assertNotIn('__LOCAL_ORIGIN__', html)
            self.assertNotIn('__INSTANCE_ID__', html)
            self.assertIn("frame-ancestors 'none'", r.headers['Content-Security-Policy'])
        with self.request('/api/action', {'action': 'new'}) as r:
            self.assertTrue(json.load(r)['ok'])
        with self.request('/api/state') as r:
            state = json.load(r)
            self.assertTrue(state['run_id'])
        with self.request('/api/action', {'action': 'abandon'}) as r:
            self.assertTrue(json.load(r)['ok'])
        self.assertIsNone(self.server.session.snapshot()['run_id'])

    def test_startup_health_proves_the_local_server_identity(self):
        check_local_server(self.server)
        with self.request('/api/health') as r:
            self.assertEqual(r.headers['X-Umacoach-Service'], 'umacoach')
            data = json.load(r)
            self.assertEqual(data['instance_id'], self.server.instance_id)
            self.assertEqual(data['service'], 'umacoach')

    def test_state_correction_http_roundtrip_and_revision_conflict(self):
        with self.request('/api/action', {'action': 'new'}):
            pass
        state=self.server.session.snapshot()
        body=dict(action='state_correct',run_id=state['run_id'],revision=state['state_revision'],
                  values=dict(mentor='俳成功',outings_used=3))
        with self.request('/api/action',body) as response:
            self.assertTrue(json.load(response)['ok'])
        with self.request('/api/state') as response:
            self.assertEqual(json.load(response)['run_state']['outings_used'],3)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/action',body)
        self.assertIn('状态已更新',json.load(caught.exception)['error'])

    def test_invalid_state_http_patch_is_atomic(self):
        self.server.session.boundary('new')
        state=self.server.session.snapshot()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/action',dict(action='state_correct',run_id=state['run_id'],
                revision=0,values=dict(outings_used=6,mentor='俳成功')))
        self.assertEqual(caught.exception.code,400)
        self.assertEqual(self.server.session.snapshot()['run_state']['mentor'],'未定')
        self.assertIsNone(self.server.session.snapshot()['run_state']['outings_used'])

    def test_unsupported_request_returns_json_instead_of_html(self):
        req = urllib.request.Request(self.server.origin+'/', method='PUT')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(req, timeout=3)
        self.assertIn('application/json', caught.exception.headers['Content-Type'])
        self.assertIn('HTTP 501', json.load(caught.exception)['error'])

    def test_missing_token_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/state', headers={'X-Umacoach-Token': ''})
        self.assertEqual(caught.exception.code, 403)

    def test_untrusted_origin_cannot_set_key(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/action', {'action': 'configure', 'key': 'attack'}, {'Origin': 'https://example.com'})
        self.assertEqual(caught.exception.code, 403)
        self.assertNotEqual(self.server.session.key, 'attack')

    def test_rebound_host_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/', headers={'Host': 'example.com'})
        self.assertEqual(caught.exception.code, 403)

    def test_window_enumeration_error_returns_useful_json(self):
        with patch('umacoach.webapp.windows', side_effect=CaptureError('枚举失败，请框选')):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request('/api/windows')
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(json.load(caught.exception)['error'], '枚举失败，请框选')

    def test_diagnostics_does_not_return_key(self):
        self.server.session.key = 'private-test'
        with self.request('/api/diagnostics') as r:
            data = r.read().decode()
        self.assertIn('pointer_bits', data)
        self.assertNotIn('private-test', data)


if __name__ == '__main__':
    unittest.main()
