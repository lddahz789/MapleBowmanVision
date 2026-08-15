import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv import bot as runtime_bot  # noqa: E402
from mbv.config import load_config  # noqa: E402
from mbv.player_tracking import PlayerTrackState  # noqa: E402
from mbv.vision import (  # noqa: E402
    Detection,
    PlayerAnchor,
    SceneFeatures,
    player_marker_observation,
)


SCENE_WIDTH = 400
SCENE_HEIGHT = 200
MARKER = (0.25, 0.50)
MARKER_SIZE = (200, 100)


def nameplate_anchor(x: float = 140.0, y: float = 110.0) -> PlayerAnchor:
    return PlayerAnchor(
        (int(round(x - 10)), int(round(y)), 20, 1),
        0.96,
        "姓名板",
        (int(round(x - 10)), int(round(y)), 20, 10),
    )


def nameplate_detection(x: float = 140.0, y: float = 110.0) -> Detection:
    return Detection(
        (int(round(x - 10)), int(round(y)), 20, 10),
        0.98,
        "own-nameplate.png",
        identity_score=0.97,
    )


def head_detection(x: float = 146.0, feet_y: float = 110.0, score: float = 0.91) -> Detection:
    # 默认头部脚底偏移为场景高度的 7%，即 14 px。
    return Detection(
        (int(round(x - 10)), int(round(feet_y - 34)), 20, 20),
        score,
        "own-head.png",
    )


def title_detection(x: float = 146.0, feet_y: float = 110.0, score: float = 0.91) -> Detection:
    # 称号脚底点位于模板上方约场景高度的 7.6%，即 15 px。
    return Detection(
        (int(round(x - 10)), int(round(feet_y + 15)), 20, 20),
        score,
        "own-title.png",
    )


class MinimapStationaryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vision = load_config(ROOT / "config.example.json")["vision"]

    def blank_bot(self) -> runtime_bot.BowmanBot:
        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.player_track = PlayerTrackState()
        instance._detect_player_nameplate = MagicMock(return_value=([], -1.0, None))
        instance._detect_player_auxiliary = MagicMock(return_value=([], -1.0, [], -1.0))
        return instance

    def seeded_bot(self) -> runtime_bot.BowmanBot:
        instance = self.blank_bot()
        seed = nameplate_anchor()
        instance.player_track.record(
            seed,
            9.9,
            velocity_alpha=1.0,
            max_displacement=100.0,
            identity_confirmed=True,
        )
        instance.player_track.last_global_at = 9.9
        return instance

    def track(
        self,
        instance: runtime_bot.BowmanBot,
        vision: dict,
        now: float,
        marker: tuple[float, float] | None,
        *,
        marker_unambiguous: bool = True,
        marker_size: tuple[int, int] | None = MARKER_SIZE,
        scene_size: tuple[int, int] = (SCENE_WIDTH, SCENE_HEIGHT),
    ) -> PlayerAnchor | None:
        scene_width, scene_height = scene_size
        return instance._track_player(
            SceneFeatures(np.zeros((scene_height, scene_width, 3), dtype=np.uint8)),
            vision,
            now,
            marker,
            marker_unambiguous=marker_unambiguous,
            marker_size=marker_size,
        )

    def establish_nameplate_reference(
        self,
        *,
        now: float = 10.0,
        marker: tuple[float, float] = MARKER,
        marker_size: tuple[int, int] = MARKER_SIZE,
    ) -> tuple[runtime_bot.BowmanBot, dict, PlayerAnchor]:
        instance = self.seeded_bot()
        vision = dict(self.vision)
        detection = nameplate_detection()
        instance._detect_player_nameplate.return_value = (
            [detection],
            detection.score,
            detection.name,
        )

        anchor = self.track(
            instance,
            vision,
            now,
            marker,
            marker_size=marker_size,
        )
        if anchor is None or anchor.source != "姓名板":
            raise AssertionError("测试未能建立可靠姓名板基准")
        instance._detect_player_nameplate.return_value = ([], -1.0, None)
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        return instance, vision, anchor

    def test_nameplate_reference_accepts_two_pixel_euclidean_boundary_without_renewal(self):
        instance, vision, original = self.establish_nameplate_reference()
        # 200x100 小地图上 (0.006, 0.016) 分别为 1.2px 与 1.6px，欧氏距离恰为 2px。
        stationary_marker = (0.256, 0.516)

        first = self.track(instance, vision, 10.10, stationary_marker)
        boundary = self.track(instance, vision, 10.20, stationary_marker)

        self.assertIsNotNone(first)
        self.assertIsNotNone(boundary)
        self.assertEqual(first.box, original.box)
        self.assertEqual(boundary.box, original.box)
        self.assertEqual(first.source, "小地图静止确认")
        self.assertEqual(boundary.source, "小地图静止确认")
        self.assertEqual(instance.player_track.last_seen_at, 10.0)
        self.assertEqual(instance.player_track.last_identity_at, 10.0)
        self.assertEqual(instance.player_track.misses, 2)
        self.assertIsNotNone(
            runtime_bot.smooth_player_attack_anchor(
                runtime_bot.player_attack_anchor(original.box, original.raw_box),
                runtime_bot.player_attack_anchor(boundary.box, boundary.raw_box),
            )
        )

        expired = self.track(instance, vision, 10.20000001, stationary_marker)
        self.assertIsNone(expired)
        self.assertEqual(instance.player_track.last_seen_at, 10.0)
        self.assertEqual(instance.player_track.last_identity_at, 10.0)

    def test_stationary_radius_uses_marker_pixels_not_old_normalized_axis_epsilon(self):
        instance, vision, original = self.establish_nameplate_reference(
            marker_size=(400, 200)
        )
        # 归一位移 0.004 大于旧的单轴 epsilon，但在 400px 宽小地图上只有 1.6px。
        anchor = self.track(
            instance,
            vision,
            10.10,
            (0.254, 0.50),
            marker_size=(400, 200),
        )

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.box, original.box)
        self.assertEqual(anchor.source, "小地图静止确认")

    def test_motion_ambiguity_loss_resize_or_expiry_clears_reference_and_blocks_generic_hold(self):
        cases = (
            ("moved_over_2px", (0.2561, 0.516), 10.10, True, MARKER_SIZE, (400, 200)),
            ("ambiguous", MARKER, 10.10, False, MARKER_SIZE, (400, 200)),
            ("missing", None, 10.10, True, MARKER_SIZE, (400, 200)),
            ("missing_size", MARKER, 10.10, True, None, (400, 200)),
            ("minimap_resized", MARKER, 10.10, True, (201, 100), (400, 200)),
            ("scene_resized", MARKER, 10.10, True, MARKER_SIZE, (401, 200)),
            ("expired", MARKER, 10.20000001, True, MARKER_SIZE, (400, 200)),
        )
        for label, marker, now, unambiguous, marker_size, scene_size in cases:
            with self.subTest(label=label):
                instance, vision, original = self.establish_nameplate_reference()
                # 即使通用视觉 miss 上限很高，静止证据失效也不能在下一帧复活旧 hold。
                vision["player_local_miss_limit"] = 10
                old_attack_anchor = runtime_bot.player_attack_anchor(
                    original.box,
                    original.raw_box,
                )

                anchor = self.track(
                    instance,
                    vision,
                    now,
                    marker,
                    marker_unambiguous=unambiguous,
                    marker_size=marker_size,
                    scene_size=scene_size,
                )
                instant_attack_anchor = (
                    runtime_bot.player_attack_anchor(anchor.box, anchor.raw_box)
                    if anchor is not None
                    else None
                )

                self.assertIsNone(anchor)
                self.assertIsNone(
                    runtime_bot.smooth_player_attack_anchor(
                        old_attack_anchor,
                        instant_attack_anchor,
                    )
                )

                follow_up = self.track(
                    instance,
                    vision,
                    now + 0.01,
                    MARKER,
                    marker_unambiguous=True,
                    marker_size=MARKER_SIZE,
                )
                self.assertIsNone(follow_up)

    def test_nameplate_without_unique_marker_cannot_create_stationary_reference(self):
        cases = (
            ("ambiguous", MARKER, False, MARKER_SIZE),
            ("missing", None, True, MARKER_SIZE),
            ("missing_size", MARKER, True, None),
        )
        for label, marker, unambiguous, marker_size in cases:
            with self.subTest(label=label):
                instance = self.seeded_bot()
                vision = dict(self.vision)
                detection = nameplate_detection()
                instance._detect_player_nameplate.return_value = (
                    [detection],
                    detection.score,
                    detection.name,
                )

                visual = self.track(
                    instance,
                    vision,
                    10.0,
                    marker,
                    marker_unambiguous=unambiguous,
                    marker_size=marker_size,
                )
                instance._detect_player_nameplate.return_value = ([], -1.0, None)
                instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
                vision["player_hold_seconds"] = 0.01
                held = self.track(instance, vision, 10.05, MARKER)

                self.assertIsNotNone(visual)
                self.assertEqual(visual.source, "姓名板")
                self.assertIsNone(held)

    def test_disabling_assist_discards_reference_before_a_later_reenable(self):
        instance, vision, _original = self.establish_nameplate_reference()
        vision["player_hold_seconds"] = 0.01
        vision["player_minimap_assist_enabled"] = False

        while_disabled = self.track(instance, vision, 10.05, (0.27, 0.50))
        vision["player_minimap_assist_enabled"] = True
        after_reenable = self.track(instance, vision, 10.10, MARKER)

        self.assertIsNone(while_disabled)
        self.assertIsNone(after_reenable)

    def test_local_head_refreshes_reference_from_its_own_visual_timestamp_without_renewal(self):
        instance, vision, _nameplate = self.establish_nameplate_reference()
        own_head = head_detection()
        instance._detect_player_auxiliary.return_value = ([own_head], own_head.score, [], -1.0)

        head_anchor = self.track(instance, vision, 10.19, MARKER)
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        first_hold = self.track(instance, vision, 10.35, (0.256, 0.516))
        boundary_hold = self.track(instance, vision, 10.39, (0.256, 0.516))

        self.assertIsNotNone(head_anchor)
        self.assertEqual(head_anchor.raw_box, own_head.box)
        self.assertTrue(head_anchor.source.startswith("头部"))
        self.assertIsNotNone(first_hold)
        self.assertIsNotNone(boundary_hold)
        self.assertEqual(first_hold.box, head_anchor.box)
        self.assertEqual(boundary_hold.box, head_anchor.box)
        self.assertEqual(first_hold.source, "小地图静止确认")
        self.assertEqual(boundary_hold.source, "小地图静止确认")
        self.assertEqual(instance.player_track.last_seen_at, 10.19)
        self.assertEqual(instance.player_track.last_identity_at, 10.0)

        expired = self.track(instance, vision, 10.39000001, (0.256, 0.516))
        self.assertIsNone(expired)
        self.assertEqual(instance.player_track.last_seen_at, 10.19)
        self.assertEqual(instance.player_track.last_identity_at, 10.0)

    def test_real_head_wins_over_stationary_reference_and_rejected_raw_head_does_not(self):
        instance, vision, _original = self.establish_nameplate_reference()
        real_head = head_detection()
        instance._detect_player_auxiliary.return_value = ([real_head], real_head.score, [], -1.0)

        visual = self.track(instance, vision, 10.10, MARKER)

        self.assertIsNotNone(visual)
        self.assertEqual(visual.raw_box, real_head.box)
        self.assertTrue(visual.source.startswith("头部"))
        self.assertEqual(instance.player_track.last_seen_at, 10.10)
        self.assertEqual(instance.player_track.last_identity_at, 10.0)

        other, other_vision, other_original = self.establish_nameplate_reference()
        rejected = Detection((190, 80, 20, 20), 0.99, "rejected-head.png")
        other._detect_player_auxiliary.return_value = ([rejected], rejected.score, [], -1.0)

        held = self.track(other, other_vision, 10.10, MARKER)

        self.assertIsNotNone(held)
        self.assertEqual(held.box, other_original.box)
        self.assertEqual(held.source, "小地图静止确认")

    def test_local_title_is_visual_priority_but_cannot_refresh_stationary_reference(self):
        instance, vision, _nameplate = self.establish_nameplate_reference()
        own_title = title_detection()
        instance._detect_player_auxiliary.return_value = ([], -1.0, [own_title], own_title.score)

        title_anchor = self.track(instance, vision, 10.19, MARKER)
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        vision["player_hold_seconds"] = 0.01
        held = self.track(instance, vision, 10.25, MARKER)

        self.assertIsNotNone(title_anchor)
        self.assertEqual(title_anchor.raw_box, own_title.box)
        self.assertTrue(title_anchor.source.startswith("称号勋章"))
        self.assertIsNone(held)
        self.assertEqual(instance.player_track.last_seen_at, 10.19)
        self.assertEqual(instance.player_track.last_identity_at, 10.0)

    def test_disabled_frame_cannot_create_a_reference_for_later_reenable(self):
        instance = self.seeded_bot()
        vision = dict(self.vision)
        vision["player_minimap_assist_enabled"] = False
        detection = nameplate_detection()
        instance._detect_player_nameplate.return_value = (
            [detection],
            detection.score,
            detection.name,
        )

        visual = self.track(instance, vision, 10.0, MARKER)
        instance._detect_player_nameplate.return_value = ([], -1.0, None)
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        vision["player_minimap_assist_enabled"] = True
        vision["player_hold_seconds"] = 0.01
        held = self.track(instance, vision, 10.05, MARKER)

        self.assertIsNotNone(visual)
        self.assertEqual(visual.source, "姓名板")
        self.assertIsNone(instance.player_track.minimap_stationary_reference)
        self.assertIsNone(held)

    def test_title_visual_cannot_hide_marker_motion_or_size_invalidation(self):
        cases = (
            ("moved", (0.27, 0.50), MARKER_SIZE),
            ("missing_size", MARKER, None),
            ("resized", MARKER, (201, 100)),
        )
        for label, marker, marker_size in cases:
            with self.subTest(label=label):
                instance, vision, _nameplate = self.establish_nameplate_reference()
                own_title = title_detection()
                instance._detect_player_auxiliary.return_value = (
                    [],
                    -1.0,
                    [own_title],
                    own_title.score,
                )

                title_anchor = self.track(
                    instance,
                    vision,
                    10.05,
                    marker,
                    marker_size=marker_size,
                )
                instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
                vision["player_hold_seconds"] = 0.01
                held = self.track(instance, vision, 10.10, MARKER)

                self.assertIsNotNone(title_anchor)
                self.assertTrue(title_anchor.source.startswith("称号勋章"))
                self.assertIsNone(held)

    def test_title_only_startup_identity_cannot_create_stationary_reference(self):
        instance = self.blank_bot()
        vision = dict(self.vision)
        own_title = title_detection(score=0.99)
        instance._detect_player_auxiliary.return_value = ([], -1.0, [own_title], own_title.score)

        first = self.track(instance, vision, 1.00, MARKER)
        second = self.track(instance, vision, 1.05, MARKER)
        title_anchor = self.track(instance, vision, 1.10, MARKER)
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        vision["player_hold_seconds"] = 0.01
        held = self.track(instance, vision, 1.15, MARKER)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(title_anchor)
        self.assertTrue(title_anchor.source.startswith("称号勋章"))
        self.assertIsNone(held)


class PlayerMarkerObservationTests(unittest.TestCase):
    def test_multiple_color_blobs_remain_visible_but_are_not_assist_safe(self):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        cv2.rectangle(image, (20, 40), (25, 45), (0, 255, 0), -1)
        cv2.rectangle(image, (70, 60), (75, 65), (0, 255, 0), -1)
        green = [{"lower": [35, 80, 80], "upper": [85, 255, 255]}]

        observation, _mask = player_marker_observation(image, green, 2, 180, (0.22, 0.42))

        self.assertIsNotNone(observation.point)
        self.assertEqual(observation.candidate_count, 2)
        self.assertFalse(observation.unambiguous)


class MinimapAssistConfigTests(unittest.TestCase):
    def write_and_load(self, config: dict) -> dict:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return load_config(path)

    def test_old_and_invalid_configs_receive_safe_defaults_and_bounds(self):
        base = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        base["vision"].pop("player_minimap_assist_enabled", None)
        base["vision"].pop("player_minimap_assist_max_seconds", None)
        loaded = self.write_and_load(base)
        self.assertTrue(loaded["vision"]["player_minimap_assist_enabled"])
        self.assertEqual(loaded["vision"]["player_minimap_assist_max_seconds"], 0.2)

        base["vision"]["player_minimap_assist_enabled"] = "false"
        base["vision"]["player_minimap_assist_max_seconds"] = 99
        loaded = self.write_and_load(base)
        self.assertTrue(loaded["vision"]["player_minimap_assist_enabled"])
        self.assertEqual(loaded["vision"]["player_minimap_assist_max_seconds"], 0.2)

        base["vision"]["player_minimap_assist_enabled"] = False
        base["vision"]["player_minimap_assist_max_seconds"] = "nan"
        loaded = self.write_and_load(base)
        self.assertFalse(loaded["vision"]["player_minimap_assist_enabled"])
        self.assertEqual(loaded["vision"]["player_minimap_assist_max_seconds"], 0.2)

        base["vision"]["player_minimap_assist_max_seconds"] = True
        loaded = self.write_and_load(base)
        self.assertEqual(loaded["vision"]["player_minimap_assist_max_seconds"], 0.2)


if __name__ == "__main__":
    unittest.main()
