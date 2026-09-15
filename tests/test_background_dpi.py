from dataclasses import replace
import unittest
from unittest.mock import MagicMock, call, patch

import cv2
import numpy as np

from mbv import background_capture as capture_module
from mbv.background_capture import BackgroundCapture, BackgroundCaptureError, print_window_frame
from mbv.window import WindowInfo


class BackgroundDpiTests(unittest.TestCase):
    def test_native_capture_is_resized_to_calibrated_client_pixels_and_context_restored(self):
        physical = WindowInfo(123, "Game", 50, 40, 960, 720)
        native = np.random.default_rng(7).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        api = MagicMock()
        api.GetWindowDpiAwarenessContext.return_value = 101
        api.SetThreadDpiAwarenessContext.side_effect = [202, 101]
        with patch.object(capture_module, "user32", api), \
             patch.object(capture_module, "client_window", return_value=physical), \
             patch.object(capture_module, "_print_native_window_frame", return_value=native) as acquire:
            result = print_window_frame(123)
        self.assertEqual(result.shape, (720, 960, 3))
        self.assertTrue(np.array_equal(result, cv2.resize(native, (960, 720), interpolation=cv2.INTER_NEAREST)))
        acquire.assert_called_once_with(123)
        api.SetThreadDpiAwarenessContext.assert_has_calls([call(101), call(202)])

    def test_dpi_aware_or_100_percent_capture_is_unchanged(self):
        physical = WindowInfo(123, "Game", 50, 40, 640, 480)
        native = np.ones((480, 640, 3), dtype=np.uint8)
        with patch.object(capture_module, "user32"), \
             patch.object(capture_module, "client_window", return_value=physical), \
             patch.object(capture_module, "_print_native_window_frame", return_value=native):
            self.assertIs(print_window_frame(123), native)

    def test_common_dpi_scales_preserve_black_game_content_without_cropping(self):
        native = np.zeros((480, 640, 3), dtype=np.uint8)
        native[20:40, 30:50] = (90, 150, 200)
        for scale in (1., 1.25, 1.5, 2.):
            width, height = int(640 * scale), int(480 * scale)
            with self.subTest(scale=scale), patch.object(capture_module, "user32"), \
                 patch.object(capture_module, "client_window", return_value=WindowInfo(123, "", 0, 0, width, height)), \
                 patch.object(capture_module, "_print_native_window_frame", return_value=native):
                result = print_window_frame(123)
            self.assertEqual(result.shape, (height, width, 3))
            self.assertTrue(np.array_equal(result, cv2.resize(native, (width, height), interpolation=cv2.INTER_NEAREST)))

    def test_failed_context_restore_cannot_return_frame(self):
        api = MagicMock()
        api.GetWindowDpiAwarenessContext.return_value = 101
        api.SetThreadDpiAwarenessContext.side_effect = [202, None]
        with patch.object(capture_module, "user32", api), \
             patch.object(capture_module, "client_window", return_value=WindowInfo(123, "", 0, 0, 640, 480)), \
             patch.object(capture_module, "_print_native_window_frame", return_value=np.ones((480, 640, 3), np.uint8)):
            with self.assertRaisesRegex(BackgroundCaptureError, "恢复"):
                print_window_frame(123)

    def test_capture_failure_restores_previous_context(self):
        api = MagicMock()
        api.GetWindowDpiAwarenessContext.return_value = 101
        api.SetThreadDpiAwarenessContext.side_effect = [202, 101]
        with patch.object(capture_module, "user32", api), \
             patch.object(capture_module, "client_window", return_value=WindowInfo(123, "", 0, 0, 640, 480)), \
             patch.object(capture_module, "_print_native_window_frame", side_effect=BackgroundCaptureError("failed")):
            with self.assertRaisesRegex(BackgroundCaptureError, "failed"):
                print_window_frame(123)
        api.SetThreadDpiAwarenessContext.assert_has_calls([call(101), call(202)])

    def test_invalid_dpi_context_or_switch_stops_before_capture(self):
        for context, previous in ((None, 202), (101, None)):
            with self.subTest(context=context, previous=previous):
                api = MagicMock()
                api.GetWindowDpiAwarenessContext.return_value = context
                api.SetThreadDpiAwarenessContext.return_value = previous
                with patch.object(capture_module, "user32", api), \
                     patch.object(capture_module, "client_window", return_value=WindowInfo(123, "", 0, 0, 640, 480)), \
                     patch.object(capture_module, "_print_native_window_frame") as acquire:
                    with self.assertRaises(BackgroundCaptureError):
                        print_window_frame(123)
                    acquire.assert_not_called()

    def test_resize_race_and_nonuniform_scaling_are_rejected(self):
        physical = WindowInfo(123, "Game", 0, 0, 960, 720)
        cases = ((replace(physical, width=1000), (480, 640, 3)), (physical, (400, 640, 3)))
        for current, shape in cases:
            with self.subTest(shape=shape, width=current.width), \
                 patch.object(capture_module, "user32"), \
                 patch.object(capture_module, "client_window", side_effect=[physical, current]), \
                 patch.object(capture_module, "_print_native_window_frame", return_value=np.ones(shape, np.uint8)):
                with self.assertRaises(BackgroundCaptureError):
                    print_window_frame(123)

    def test_parent_rejects_wrong_size_or_malformed_result_and_closes_worker(self):
        for frame in (None, np.zeros((480, 640, 4), np.uint8), np.zeros((480, 640, 3), np.float32),
                      np.zeros((320, 640, 3), np.uint8)):
            with self.subTest(shape=getattr(frame, "shape", None)):
                capture = BackgroundCapture()
                capture._process = MagicMock(pid=123)
                capture._connection = MagicMock()
                capture._connection.recv.return_value = (True, frame)
                with patch.object(capture, "close") as close:
                    with self.assertRaisesRegex(BackgroundCaptureError, "尺寸"):
                        capture.capture(WindowInfo(123, "", 0, 0, 640, 480))
                    close.assert_called_once()
