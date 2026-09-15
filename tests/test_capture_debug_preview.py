import unittest
from unittest.mock import MagicMock, patch

from mbv.overlay import capture_debug_preview, paint_capture_debug, RuntimeOverlay


class CaptureDebugPreviewTests(unittest.TestCase):
    def test_default_off_and_context_restores_after_cancel(self):
        canvas = MagicMock()
        with patch.object(RuntimeOverlay, "_paint_canvas") as paint:
            paint_capture_debug(canvas, 800, 600)
            paint.assert_not_called()
            with self.assertRaises(RuntimeError):
                with capture_debug_preview({"show_calibration": True}):
                    paint_capture_debug(canvas, 800, 600)
                    raise RuntimeError("cancel")
            paint.assert_called_once()
            paint.reset_mock()
            paint_capture_debug(canvas, 800, 600)
            paint.assert_not_called()

    def test_snapshot_is_independent_and_does_not_clear_frozen_image(self):
        state = {"show_calibration": True, "marker_screen": (20, 30)}
        canvas = MagicMock()
        with capture_debug_preview(state), patch.object(RuntimeOverlay, "_paint_canvas") as paint:
            state["marker_screen"] = (90, 90)
            paint_capture_debug(canvas, 800, 600)
            self.assertEqual(paint.call_args.args[2]["marker_screen"], (20, 30))
            self.assertFalse(paint.call_args.kwargs["clear"])

    def test_zoom_moves_only_items_inside_source_roi(self):
        canvas = MagicMock()
        canvas.find_all.side_effect = [(1,), (1, 2, 3)]
        canvas.bbox.side_effect = lambda item: (12, 22, 18, 28) if item == 2 else (0, 0, 5, 5)
        with capture_debug_preview({"show_calibration": True}), patch.object(RuntimeOverlay, "_paint_canvas"):
            paint_capture_debug(canvas, 800, 600, ((10, 20, 20, 20), (100, 200, 80, 80)))
        canvas.scale.assert_called_once_with(2, 10, 20, 4., 4.)
        canvas.move.assert_called_once_with(2, 90, 180)
        canvas.delete.assert_called_once_with(3)
