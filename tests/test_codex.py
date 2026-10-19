import base64
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from umacoach import codex_backend as cb
from umacoach.session import Session


def observation():
    return dict(scene='run_start', confidence=.99, character='貴婦人', date_text='', turn_index=None,
                evidence=['模拟开局'], recommendation='查看当前训练预览', reason='测试', missing=[], alternatives=[], guide_refs=[])


class CodexBackend(unittest.TestCase):
    def test_account_mode_strips_api_auth_but_preserves_user_home(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-api-secret', 'CODEX_ACCESS_TOKEN': 'test-token', 'CODEX_HOME': '/existing-codex-home'}):
            env = cb.cli_env()
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_ACCESS_TOKEN', env)
        self.assertEqual(env['CODEX_HOME'], '/existing-codex-home')

    def test_api_login_is_not_accepted_as_chatgpt_login(self):
        result = subprocess.CompletedProcess([], 0, stdout='Logged in using an API key: test-api-secret', stderr='')
        with patch.object(cb, 'binary', return_value='/fake/codex'), patch.object(cb.subprocess, 'run', return_value=result):
            status = cb.login_status()
        self.assertEqual(status, 'login_required')
        self.assertNotIn('secret', status)

    def test_recognizes_official_chatgpt_login_status(self):
        result = subprocess.CompletedProcess([], 0, stdout='', stderr='Logged in using ChatGPT')
        with patch.object(cb, 'binary', return_value='/fake/codex'), patch.object(cb.subprocess, 'run', return_value=result):
            self.assertEqual(cb.login_status(), 'ready')

    def test_analysis_requires_chatgpt_auth_before_request(self):
        with patch.object(cb, 'login_status', return_value='login_required'), patch.object(cb.subprocess, 'run') as run:
            with self.assertRaises(cb.CodexError):
                cb.analyze_codex(Image.new('RGB', (20, 20)), {}, Path('absent.md'))
        run.assert_not_called()

    def test_image_schema_readonly_and_temporary_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def run(command, **kwargs):
                self.assertIn('--image', command)
                self.assertIn('--output-schema', command)
                self.assertIn('--ephemeral', command)
                self.assertIn('read-only', command)
                self.assertIn('forced_login_method="chatgpt"', command)
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)
                picture = Path(command[command.index('--image')+1])
                self.assertTrue(picture.exists())
                definition = json.loads(Path(command[command.index('--output-schema')+1]).read_text())
                self.assertFalse(definition['additionalProperties'])
                Path(command[command.index('-o')+1]).write_text(json.dumps(observation()), 'utf-8')
                return subprocess.CompletedProcess(command, 0, stdout='', stderr='')
            with patch.object(cb, 'ROOT', root), patch.object(cb, 'binary', return_value='/fake/codex'), patch.object(cb, 'login_status', return_value='ready'), patch.object(cb.subprocess, 'run', side_effect=run):
                result = cb.analyze_codex(Image.new('RGB', (20, 20)), {}, root/'guide.md')
            self.assertEqual(result['recommendation'], '查看当前训练预览')
            self.assertEqual(list(root.rglob('screen.jpg')), [])

    def test_exit_zero_without_result_file_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cb, 'ROOT', Path(tmp)), patch.object(cb, 'binary', return_value='/fake/codex'), patch.object(cb, 'login_status', return_value='ready'), patch.object(cb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                with self.assertRaises(cb.CodexError):
                    cb.analyze_codex(Image.new('RGB', (20, 20)), {}, Path(tmp)/'guide.md')

    def test_quota_failure_is_reported_without_raw_cli_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.CompletedProcess([], 1, '', 'Usage limit reached test-api-secret')
            with patch.object(cb, 'ROOT', Path(tmp)), patch.object(cb, 'binary', return_value='/fake/codex'), patch.object(cb, 'login_status', return_value='ready'), patch.object(cb.subprocess, 'run', return_value=result):
                with self.assertRaises(cb.CodexError) as caught:
                    cb.analyze_codex(Image.new('RGB', (20, 20)), {}, Path(tmp)/'guide.md')
            self.assertEqual(caught.exception.reason, 'quota')
            self.assertNotIn('secret', str(caught.exception))

    def test_bad_download_is_never_extracted_or_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cb, 'INSTALL', Path(tmp)/'codex'), patch.object(cb.sys, 'platform', 'win32'), patch.object(cb, 'binary', return_value=None), patch.object(cb.urllib.request, 'urlopen', return_value=BytesIO(b'wrong package')), patch.object(cb.tarfile, 'open') as extract:
                with self.assertRaises(cb.CodexError):
                    cb.install(lambda _: None)
            extract.assert_not_called()
            self.assertFalse((Path(tmp)/'codex').exists())

    def test_login_uses_official_cli_with_forced_account_mode(self):
        with patch.object(cb, 'binary', return_value='/fake/codex'), patch.object(cb.subprocess, 'Popen') as start:
            cb.open_login()
        self.assertEqual(start.call_args.args[0], ['/fake/codex', '-c', 'forced_login_method="chatgpt"', 'login'])


class AccountSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Session(Path(self.tmp.name))
        self.session.key = ''

    def wait(self):
        deadline = time.monotonic()+3
        while (self.session.busy or self.session.codex_busy) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.session.busy or self.session.codex_busy)

    def test_account_is_default_but_does_not_analyze_without_login(self):
        self.assertEqual(self.session.provider, 'codex')
        self.assertFalse(self.session.analysis_ready())
        self.session.key = 'api-key-must-not-enable-account-mode'
        self.assertFalse(self.session.analysis_ready())

    def test_account_produces_advice_without_api_key(self):
        s = self.session
        s.codex_ready = True
        s.hwnd = 123
        with patch('umacoach.session.capture', return_value=Image.new('RGB', (100, 100))), patch.object(cb, 'analyze_codex', return_value=observation()) as codex, patch('umacoach.session.analyze') as api:
            s.read_now()
            self.wait()
        self.assertTrue(s.snapshot()['analysis_ready'])
        self.assertEqual(s.snapshot()['result']['recommendation'], '查看当前训练预览')
        codex.assert_called_once()
        api.assert_not_called()

    def test_login_failure_cannot_fall_back_to_api(self):
        s = self.session
        s.codex_ready = True
        s.key = 'unused-api-key'
        with patch.object(cb, 'analyze_codex', side_effect=cb.CodexError('需要登录', 'login')), patch('umacoach.session.analyze') as api:
            with s.lock:
                s.submit(Image.new('RGB', (20, 20)), False)
            self.wait()
        self.assertFalse(s.codex_ready)
        self.assertIsNone(s.result)
        api.assert_not_called()

    def test_provider_switch_keeps_capture_target(self):
        self.session.hwnd = 123
        self.session.set_provider('api')
        self.assertEqual(self.session.hwnd, 123)
        self.assertEqual(self.session.provider, 'api')

    def test_official_login_is_prepared_asynchronously(self):
        with patch.object(cb, 'binary', return_value=None), patch.object(cb, 'install') as install, patch.object(cb, 'open_login') as login:
            self.session.login_codex()
            self.wait()
        install.assert_called_once()
        login.assert_called_once()
        self.assertEqual(self.session.codex_state, 'login_required')

    def test_login_check_enables_account_analysis_without_key(self):
        with patch.object(cb, 'login_status', return_value='ready'):
            self.session.check_codex()
        self.assertTrue(self.session.analysis_ready())
        self.assertFalse(self.session.snapshot()['has_key'])


if __name__ == '__main__':
    unittest.main()
