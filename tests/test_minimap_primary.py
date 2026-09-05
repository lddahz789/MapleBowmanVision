from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from mbv.bot import BowmanBot
from mbv.config import load_config, save_config
from mbv.minimap_localization import background_is_stable, background_snapshot
from mbv.player_tracking import PlayerTrackState
from mbv.strategies import get_strategy
from mbv.strategies.base import StrategyActionContext, StrategyDecision, TargetSelectionContext
from mbv.vision import Detection, PlayerAnchor, SceneFeatures
from mbv.window import WindowInfo


ROOT = Path(__file__).resolve().parents[1]
MARKER = (0.5, 0.5)
SIZE = (200, 100)


class BackgroundGuardTests(unittest.TestCase):
    def setUp(self):
        self.image = np.random.default_rng(8).integers(0, 256, (180, 320), dtype=np.uint8)

    def test_scattered_stable_tiles_allow_partial_occlusion(self):
        current = self.image.copy()
        current[60:120, 80:240] = 0
        self.assertTrue(background_is_stable(self.image, current))

    def test_camera_shift_blank_large_effect_resize_and_absent_reference_are_rejected(self):
        for current in (np.roll(self.image, 3, axis=1), np.zeros_like(self.image),
                        self.image[:90], 255 - self.image):
            self.assertFalse(background_is_stable(self.image, current))
        self.assertFalse(background_is_stable(None, self.image))
        self.assertFalse(background_is_stable(np.zeros_like(self.image), np.zeros_like(self.image)))

    def test_only_one_stable_strip_is_not_enough(self):
        current = np.zeros_like(self.image)
        current[:60] = self.image[:60]
        self.assertFalse(background_is_stable(self.image, current))


class OcclusionTrackingTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config.example.json")
        self.instance = BowmanBot.__new__(BowmanBot)
        self.instance.player_track = PlayerTrackState()
        self.instance._detect_player_nameplate = MagicMock(return_value=(
            [Detection((130, 110, 20, 10), 0.98, "own.png", identity_score=0.97)], 0.98, "own.png"))
        self.instance._detect_player_auxiliary = MagicMock(return_value=([], -1.0, [], -1.0))
        self.scene = SceneFeatures(np.random.default_rng(8).integers(0, 256, (200, 400, 3), dtype=np.uint8))
        self.track(9.9)
        self.original = self.track(10.0)
        self.assertIsNotNone(self.original)
        self.instance._detect_player_nameplate.return_value = ([], -1.0, None)

    def track(self, now, marker=MARKER, scene=None, unique=True):
        return self.instance._track_player(
            scene or self.scene, self.config["vision"], now, marker,
            marker_unambiguous=unique, marker_size=SIZE,
        )

    def test_stable_scene_extends_occlusion_without_refreshing_any_identity_time(self):
        for now in (10.4, 11.0, 12.0, 13.0):
            result = self.track(now)
            self.assertIsNotNone(result)
            self.assertEqual(result.box, self.original.box)
            self.assertEqual(result.source, "小地图静止确认")
            self.assertEqual(self.instance.player_track.last_seen_at, 10.0)
            self.assertEqual(self.instance.player_track.last_identity_at, 10.0)
            self.assertEqual(self.instance.player_track.minimap_navigation_seen_at, 10.0)
        self.assertIsNone(self.track(13.01))
        self.assertIsNone(self.track(13.1))

    def test_camera_shift_invalidates_and_cannot_revive_on_return(self):
        shifted = SceneFeatures(np.roll(self.scene.scene, 10, axis=1))
        self.assertIsNone(self.track(10.4, scene=shifted))
        self.assertIsNone(self.track(10.5))

    def test_long_occlusion_rejects_half_pixel_exceedance_and_return(self):
        self.assertIsNone(self.track(10.4, marker=(0.503, 0.5)))
        self.assertIsNone(self.track(10.5))

    def test_ambiguous_or_missing_marker_invalidates_extended_anchor(self):
        self.assertIsNone(self.track(10.4, unique=False))
        self.assertIsNone(self.track(10.5))

    def test_disabling_extended_hold_preserves_short_hold(self):
        self.config["vision"]["player_minimap_occlusion_seconds"] = 0.0
        self.assertIsNotNone(self.track(10.1))
        self.assertIsNone(self.track(10.4))

    def test_background_reference_is_owned_not_mutable_frame_buffer(self):
        reference = self.instance.player_track.minimap_stationary_reference.background
        expected = reference.copy()
        self.scene.scene[:] = 0
        np.testing.assert_array_equal(reference, expected)


class MinimapNavigationTests(unittest.TestCase):
    def setUp(self):
        self.instance = BowmanBot.__new__(BowmanBot)
        bot = self.instance
        bot.config = load_config(ROOT / "config.example.json")
        bot.config["strategy"]["active"] = "stationary_attack"
        bot.config["recognition"].update(platform_center={"x": 0.5, "y": 0.5})
        bot.strategy = get_strategy("stationary_attack")
        bot.armed = bot.input_authorized = bot.background_input = True
        bot.started_at = 1.0
        bot.marker_last_seen = 10.0
        bot.last_nameplate_seen_at = 8.0
        bot.last_attack_anchor = (140.0, 110.0)
        bot.last_target_seen = bot.last_pickup = 0.0
        bot.direction = "right"
        bot.live_marker_unambiguous = True
        bot.player_track = PlayerTrackState()
        bot.player_track.record(PlayerAnchor((130, 110, 20, 1), 0.95, "姓名板", (130, 110, 20, 10)), 8.0,
                                velocity_alpha=1.0, max_displacement=100.0)
        bot.player_track.minimap_navigation_seen_at = 8.0
        for method in ("stop_move", "_interrupt_step", "_reset_player_lost_recovery",
                       "recover_player_nameplate", "face_and_attack", "_move_with_feedback",
                       "jump_to_safe", "down_jump_to_safe", "periodic_step", "disarm"):
            setattr(bot, method, MagicMock())
        bot._try_auto_potion = MagicMock(return_value=False)
        bot._try_auto_buff = MagicMock(return_value=False)
        bot._advance_periodic_step = MagicMock(return_value=False)
        bot.keyboard = MagicMock()

    def act(self, marker=MARKER, now=10.0, player=None):
        with patch("mbv.bot.user32.IsWindow", return_value=True), patch("mbv.bot.user32.IsIconic", return_value=False):
            self.instance._act(WindowInfo(123, "NewMaple", 0, 0, 800, 600),
                               1.0, 1.0, marker, player, (160, 100, 20, 20), None, 400, True, now, 200)

    def test_occluded_player_returns_using_minimap_not_stale_target_or_recovery(self):
        self.act(marker=(0.7, 0.5))
        self.instance._move_with_feedback.assert_called_once_with("left", (0.7, 0.5), 10.0, "RETURN_CENTER_LEFT")
        self.instance.face_and_attack.assert_not_called()
        self.instance.recover_player_nameplate.assert_not_called()
        self.assertIsNone(self.instance.last_attack_anchor)

    def test_at_center_waits_without_attacking_or_due_periodic_step(self):
        self.instance.last_periodic_step = -40.0
        self.act()
        self.assertEqual(self.instance.state, "MINIMAP_WAITING_VISUAL")
        self.instance.face_and_attack.assert_not_called()
        self.instance.periodic_step.assert_not_called()
        self.instance.stop_move.assert_called()

    def test_missing_and_ambiguous_marker_release_movement(self):
        self.act(marker=None)
        self.instance.stop_move.assert_called()
        self.instance.live_marker_unambiguous = False
        self.act(marker=(0.7, 0.5))
        self.instance._move_with_feedback.assert_not_called()
        self.instance.recover_player_nameplate.assert_not_called()

    def test_navigation_expires_without_renewing_from_minimap_frames(self):
        self.act(marker=(0.7, 0.5), now=19.0)
        self.assertEqual(self.instance.state, "MINIMAP_VISUAL_TIMEOUT")
        self.instance._move_with_feedback.assert_not_called()
        self.instance.recover_player_nameplate.assert_not_called()

    def test_no_paired_unique_marker_never_authorizes_minimap_movement(self):
        self.instance.player_track.minimap_navigation_seen_at = 0.0
        self.act(marker=(0.7, 0.5))
        self.instance._move_with_feedback.assert_not_called()

    def test_master_switch_off_preserves_legacy_recovery(self):
        self.instance.config["vision"]["player_minimap_assist_enabled"] = False
        self.act(marker=(0.7, 0.5))
        self.instance.recover_player_nameplate.assert_called_once_with(10.0)

    def test_no_nameplate_identity_does_not_authorize_navigation(self):
        self.instance.player_track.nameplate_identity_established = False
        self.act(marker=(0.7, 0.5))
        self.instance._move_with_feedback.assert_not_called()

    def test_live_head_location_prevents_unnecessary_nameplate_recovery(self):
        self.instance.player_track.last_seen_at = 10.0
        self.act(player=(130, 110, 20, 1))
        self.instance.face_and_attack.assert_called_once()
        self.instance.recover_player_nameplate.assert_not_called()

    def test_common_gate_rejects_attack_from_a_misbehaving_minimap_strategy(self):
        self.instance.strategy = MagicMock()
        self.instance.strategy.decide.return_value = StrategyDecision("attack", "ATTACK", attack_key="m")
        self.act()
        self.instance.face_and_attack.assert_not_called()
        self.assertEqual(self.instance.state, "MINIMAP_WAITING_VISUAL")

    def test_potion_and_buff_keep_priority_over_navigation(self):
        for method in ("_try_auto_potion", "_try_auto_buff"):
            with self.subTest(method=method):
                getattr(self.instance, method).return_value = True
                self.act(marker=(0.7, 0.5))
                self.instance._move_with_feedback.assert_not_called()
                getattr(self.instance, method).return_value = False

    def test_all_strategies_handle_minimap_only_without_visual_target_selection(self):
        context = StrategyActionContext(
            marker=(0.7, 0.5), player_box=None, player_anchor=None,
            target_box=(160, 100, 20, 20), chase_box=None, combat_width=400,
            has_monster_candidates=True, now=10.0, last_target_seen=0.0,
            last_pickup=0.0, direction=None, behavior={}, settings={},
            recognition={"platform_center": {"x": 0.5, "y": 0.5}}, minimap_only=True,
        )
        for key in ("stationary_attack", "bowman_dynamic", "throwing_star_safe"):
            with self.subTest(key=key):
                strategy = get_strategy(key)
                current = context
                if key == "throwing_star_safe":
                    current = replace(context, settings={"use_safe_output_area": True},
                                      recognition={"throwing_star_safe_output_area": {
                                          "space": "minimap", "x": 0.45, "y": 0.45, "w": 0.1, "h": 0.1}})
                self.assertEqual(strategy.decide(current).action, "move")
                self.assertEqual(strategy.decide(replace(current, marker=MARKER)).action, "stop")
                self.assertEqual(strategy.decide(replace(current, marker=None)).action, "stop")
                selected = strategy.select_targets(TargetSelectionContext(
                    [], None, None, None, 400, 200, "right", {}, {}))
                self.assertIsNone(selected.target)

    def test_new_limits_default_and_normalize_and_persist(self):
        config = self.instance.config
        self.assertEqual(config["vision"]["player_minimap_occlusion_seconds"], 3.0)
        self.assertEqual(config["vision"]["player_minimap_navigation_seconds"], 10.0)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for raw, expected in ((100, 5.0), (-1, 0.0), (True, 3.0), ("bad", 3.0)):
                config["vision"]["player_minimap_occlusion_seconds"] = raw
                save_config(path, config)
                self.assertEqual(load_config(path)["vision"]["player_minimap_occlusion_seconds"], expected)


if __name__ == "__main__":
    unittest.main()
