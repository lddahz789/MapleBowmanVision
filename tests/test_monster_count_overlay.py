import unittest
from unittest.mock import MagicMock

from mbv.overlay import RuntimeOverlay


class MonsterCountOverlayTests(unittest.TestCase):
    def test_count_and_zero_are_visible_and_respect_debug_switches(self):
        for count in (0, 3):
            for hidden in (False, True):
                for enabled in (False, True):
                    with self.subTest(count=count, hidden=hidden, enabled=enabled):
                        canvas = MagicMock()
                        RuntimeOverlay._paint_canvas(None, canvas, {
                            "show_calibration": enabled,
                            "debug_hidden_items": ("monster",) if hidden else (),
                            "monster_count": count, "eligible_monster_count": count,
                        }, 800, 600)
                        texts = [c.kwargs.get("text", "") for c in canvas.create_text.call_args_list]
                        self.assertEqual(f"怪物：{count}  ·  范围内：{count}" in texts,
                                         enabled and not hidden)
