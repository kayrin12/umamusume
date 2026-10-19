import ctypes
from ctypes import wintypes
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from umacoach.capture import (CaptureError, _api, capture_region, foreground_window,
                             selection_box, windows)


class FakeUser:
    def EnumWindows(self, callback, _):
        for handle in (2**40, 10, 11):
            callback(handle, 0)
        return 1
    def IsWindowVisible(self, handle):
        return handle != 11
    def GetWindowTextLengthW(self, handle):
        return len(self.title(handle))
    def GetWindowTextW(self, handle, buffer, _):
        buffer.value = self.title(handle)
        return len(buffer.value)
    def GetClassNameW(self, handle, buffer, _):
        buffer.value = 'UnityWndClass'
        return len(buffer.value)
    def title(self, handle):
        return '' if handle == 2**40 else '其他窗口'
    def GetForegroundWindow(self):
        return 7
    def GetAncestor(self, handle, kind):
        return 2**40


class Capture(unittest.TestCase):
    def test_visible_untitled_game_is_not_dropped_and_handle_is_preserved(self):
        api = SimpleNamespace(user=FakeUser(), callback=lambda fn: fn)
        with patch('umacoach.capture.sys.platform', 'win32'), patch('umacoach.capture._api', return_value=api):
            result = windows()
        self.assertIn((2**40, '无标题窗口 · UnityWndClass'), result)
        self.assertEqual(len(result), 2)

    def test_enum_failure_is_reported_instead_of_silent_empty_list(self):
        user = FakeUser()
        user.EnumWindows = lambda *_: 0
        with patch('umacoach.capture.sys.platform', 'win32'), patch('umacoach.capture._api', return_value=SimpleNamespace(user=user, callback=lambda fn: fn)):
            with self.assertRaises(CaptureError):
                windows()

    def test_foreground_selection_does_not_require_title_or_enum_membership(self):
        with patch('umacoach.capture._api', return_value=SimpleNamespace(user=FakeUser())):
            handle, title = foreground_window()
        self.assertEqual(handle, 2**40)
        self.assertIn('UnityWndClass', title)

    def test_64bit_api_prototypes_are_explicit(self):
        class Call:
            pass
        class User:
            def __getattr__(self, name):
                item = Call()
                setattr(self, name, item)
                return item
        user = User()
        with patch('umacoach.capture.sys.platform', 'win32'), patch.object(ctypes, 'WinDLL', return_value=user, create=True), patch.object(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE, create=True):
            api = _api.__wrapped__()
        self.assertIs(api.user.GetForegroundWindow.restype, wintypes.HWND)
        self.assertIs(api.user.GetAncestor.restype, wintypes.HWND)
        self.assertIs(api.user.GetWindowTextW.argtypes[0], wintypes.HWND)

    def test_selection_scales_to_negative_monitor_coordinates(self):
        box = selection_box([100, 50, 200, 100], [800, 450], [-800, 0, 1600, 900])
        self.assertEqual(box, (-600, 100, -200, 300))

    def test_invalid_selection_never_expands_to_full_screen(self):
        for rect in ([-1, 0, 100, 100], [700, 0, 200, 200], [0, 0, 1, 1], [True, 0, 100, 100]):
            with self.subTest(rect=rect), self.assertRaises(CaptureError):
                selection_box(rect, [800, 450], [0, 0, 1600, 900])

    def test_region_capture_reads_only_confirmed_box(self):
        image = Image.new('RGB', (200, 300))
        with patch('umacoach.capture.desktop_bounds', return_value=(-800, 0, 1600, 900)), patch('PIL.ImageGrab.grab', return_value=image) as grab:
            result = capture_region((-600, 100, -400, 400))
        self.assertIs(result, image)
        grab.assert_called_once_with(bbox=(-600, 100, -400, 400), all_screens=True)

    def test_monitor_layout_change_requires_new_selection(self):
        with patch('umacoach.capture.desktop_bounds', return_value=(0, 0, 800, 600)), patch('PIL.ImageGrab.grab') as grab:
            with self.assertRaises(CaptureError):
                capture_region((-600, 100, -400, 400))
        grab.assert_not_called()


if __name__ == '__main__':
    unittest.main()
