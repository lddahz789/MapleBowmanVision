import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, call, patch

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv import bot as runtime_bot  # noqa: E402
from mbv.buffs import (  # noqa: E402
    AutoBuffController,
    BUFF_KEY_HOLD_SECONDS,
    BUFF_PRE_CAST_SECONDS,
)
from mbv.input import Keyboard, vk_for  # noqa: E402
from mbv.config import load_config  # noqa: E402
from mbv.player_tracking import PlayerTrackState  # noqa: E402
from mbv.potion import AutoPotionController  # noqa: E402
from mbv.vision import (  # noqa: E402
    Detection,
    PlayerAnchor,
    SceneFeatures,
    Template,
    deduplicate_nameplate_detections,
    find_detections,
    load_templates,
    nameplate_identity_similarity,
    player_anchor_from_detection,
    player_tracking_roi,
    verify_nameplate_identities,
)
from mbv.window import WindowInfo  # noqa: E402


class PlayerTrackingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

    def test_local_detection_returns_global_scene_coordinates(self):
        rng = np.random.default_rng(1234)
        template_image = rng.integers(0, 256, (14, 18, 3), dtype=np.uint8)
        scene = np.zeros((120, 180, 3), dtype=np.uint8)
        scene[52:66, 83:101] = template_image
        template = Template("player.png", template_image, np.full((14, 18), 255, dtype=np.uint8))
        features = SceneFeatures(scene)

        detections, score, _name = find_detections(
            features,
            [template],
            0.99,
            1.0,
            search_roi=(60, 30, 70, 60),
        )

        self.assertGreater(score, 0.99)
        self.assertEqual(detections[0].box, (83, 52, 18, 14))
        self.assertIn((1.0, (60, 30, 70, 60)), features._scaled)
        self.assertIn((1.0, (60, 30, 70, 60)), features._opponent)

    def test_half_scale_local_roi_uses_same_sampling_grid_as_full_scene(self):
        rng = np.random.default_rng(5678)
        template_image = rng.integers(0, 256, (18, 22, 3), dtype=np.uint8)
        scene = np.zeros((140, 220, 3), dtype=np.uint8)
        scene[58:76, 93:115] = template_image
        template = Template("player.png", template_image, np.full((18, 22), 255, dtype=np.uint8))
        features = SceneFeatures(scene)

        full, full_score, _name = find_detections(
            features,
            [template],
            0.5,
            0.5,
            structure_weight=0.55,
        )
        local, local_score, _name = find_detections(
            features,
            [template],
            0.5,
            0.5,
            structure_weight=0.55,
            search_roi=(61, 31, 100, 80),
        )

        self.assertTrue(full)
        self.assertTrue(local)
        self.assertAlmostEqual(local_score, full_score, places=5)
        self.assertEqual(local[0].box, full[0].box)

    def test_tracking_roi_slides_at_scene_edges_without_shrinking(self):
        left = player_tracking_roi((0.0, 0.0), 1000, 500, 0.36, 0.24, 0.18)
        right = player_tracking_roi((999.0, 499.0), 1000, 500, 0.36, 0.24, 0.18)

        self.assertEqual(left, (0, 0, 360, 210))
        self.assertEqual(right, (640, 290, 360, 210))

    def test_nameplate_identity_similarity_uses_name_glyphs_not_shared_plate(self):
        template = np.full((24, 64, 3), (180, 70, 20), dtype=np.uint8)
        same = template.copy()
        other = template.copy()
        cv2.putText(template, "AB", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.putText(same, "AB", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.putText(other, "XY", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

        same_score = nameplate_identity_similarity(template, same)
        other_score = nameplate_identity_similarity(template, other)

        self.assertGreater(same_score, 0.9)
        self.assertLess(other_score, same_score - 0.25)

    def test_nameplate_identity_similarity_tolerates_one_pixel_candidate_shift(self):
        template = np.full((24, 64, 3), (180, 70, 20), dtype=np.uint8)
        shifted = np.full_like(template, (180, 70, 20))
        cv2.putText(template, "AB", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.putText(shifted, "AB", (19, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

        self.assertGreater(nameplate_identity_similarity(template, shifted), 0.85)

    def test_nameplate_identity_similarity_ignores_transparent_white_border(self):
        template = np.full((26, 64, 3), 255, dtype=np.uint8)
        candidate = template.copy()
        alpha = np.zeros((26, 64), dtype=np.uint8)
        alpha[3:23, 10:54] = 255
        template[3:23, 10:54] = (180, 70, 20)
        candidate[3:23, 10:54] = (180, 70, 20)
        cv2.putText(template, "AB", (18, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.putText(candidate, "AB", (18, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)

        self.assertGreater(nameplate_identity_similarity(template, candidate, alpha), 0.9)

    def test_nameplate_deduplication_prefers_identity_over_raw_template_score(self):
        invalid = Detection((100, 50, 61, 26), 0.95, "wide.png", identity_score=0.0)
        valid = Detection((108, 50, 44, 26), 0.90, "valid.png", identity_score=0.82)

        kept = deduplicate_nameplate_detections([invalid, valid], nms_iou=0.35)

        self.assertEqual(kept, [valid])

    def test_nameplate_identity_verification_scores_each_candidate_at_full_resolution(self):
        template_image = np.full((24, 64, 3), (180, 70, 20), dtype=np.uint8)
        own = template_image.copy()
        other = template_image.copy()
        cv2.putText(template_image, "AB", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.putText(own, "AB", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.putText(other, "XY", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        scene = np.zeros((80, 160, 3), dtype=np.uint8)
        scene[10:34, 10:74] = own
        scene[45:69, 80:144] = other
        template = Template("player.png", template_image, np.full((24, 64), 255, dtype=np.uint8))

        verified = verify_nameplate_identities(
            scene,
            [
                Detection((10, 10, 64, 24), 0.9, "player.png"),
                Detection((80, 45, 64, 24), 0.99, "player.png"),
            ],
            [template],
        )

        self.assertGreater(verified[0].identity_score, 0.9)
        self.assertLess(verified[1].identity_score, verified[0].identity_score - 0.25)

    def test_template_anchor_metadata_is_loaded_and_controls_player_feet(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "player.png"
            image = np.full((12, 18, 4), 255, dtype=np.uint8)
            ok, encoded = cv2.imencode(".png", image)
            self.assertTrue(ok)
            encoded.tofile(path)
            path.with_suffix(".anchor.json").write_text(
                json.dumps({"version": 1, "anchor_offset": [7.0, 26.0]}),
                encoding="utf-8",
            )

            template = load_templates(directory)[0]
            detection = Detection(
                (100, 80, 18, 12),
                0.95,
                template.name,
                anchor_offset=template.anchor_offset,
            )
            anchor = player_anchor_from_detection(detection, "姓名板", 300, 0.07, 0.076)

        self.assertEqual(template.anchor_offset, (7.0, 26.0))
        self.assertAlmostEqual(anchor.box[0] + anchor.box[2] / 2.0, 107.0, delta=0.5)
        self.assertEqual(anchor.box[1], 106)

    def test_track_state_predicts_motion_and_resets_velocity_on_source_change(self):
        state = PlayerTrackState()
        first = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        second = PlayerAnchor((110, 80, 20, 1), 0.9, "姓名板", (110, 80, 20, 10))
        head = PlayerAnchor((112, 80, 20, 1), 0.9, "头部", (112, 40, 20, 20))
        state.record(first, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        state.record(second, 10.5, velocity_alpha=1.0, max_displacement=100.0)

        self.assertEqual(state.velocity, (40.0, 0.0))
        predicted = state.predicted_point(10.7, 0.2)
        self.assertAlmostEqual(predicted[0], 128.0)
        self.assertAlmostEqual(predicted[1], 80.0)
        state.record(head, 11.0, velocity_alpha=1.0, max_displacement=100.0)
        self.assertEqual(state.velocity, (0.0, 0.0))

    def test_auxiliary_record_preserves_prior_identity_without_confirming_a_new_one(self):
        state = PlayerTrackState()
        nameplate = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        head = PlayerAnchor((92, 80, 20, 1), 0.9, "头部", (92, 40, 20, 20))
        state.record(nameplate, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        state.record(
            head,
            10.2,
            velocity_alpha=1.0,
            max_displacement=100.0,
            identity_confirmed=False,
        )

        self.assertEqual(state.last_identity_at, 10.0)
        self.assertEqual(state.mode, "OCCLUDED")
        self.assertTrue(state.has_confirmed_identity())

    def _tracker_bot(self) -> runtime_bot.BowmanBot:
        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.player_track = PlayerTrackState()
        instance._detect_player_nameplate = MagicMock()
        instance._detect_player_auxiliary = MagicMock()
        return instance

    def test_stable_tracking_uses_local_nameplate_scan_without_auxiliary(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.last_auxiliary_at = 10.0
        instance._detect_player_nameplate.return_value = (
            [Detection((92, 80, 20, 10), 0.95, "player.png", identity_score=0.96)],
            0.95,
            "player.png",
        )
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchor = instance._track_player(scene, self.config["vision"], 10.1)

        search_roi = instance._detect_player_nameplate.call_args.args[2]
        self.assertIsNotNone(search_roi)
        self.assertEqual(anchor.raw_box, (92, 80, 20, 10))
        instance._detect_player_auxiliary.assert_not_called()

    def test_second_local_miss_escalates_to_same_frame_global_scan(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.last_auxiliary_at = 10.0
        instance.player_track.misses = 1
        recovered = Detection((94, 80, 20, 10), 0.96, "player.png", identity_score=0.97)
        instance._detect_player_nameplate.side_effect = [
            ([], -1.0, None),
            ([recovered], 0.96, "player.png"),
        ]
        instance._detect_player_auxiliary.side_effect = [
            ([], -1.0, [], -1.0),
            ([], -1.0, [], -1.0),
        ]
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchor = instance._track_player(scene, self.config["vision"], 10.1)

        first_roi = instance._detect_player_nameplate.call_args_list[0].args[2]
        second_roi = instance._detect_player_nameplate.call_args_list[1].args[2]
        self.assertIsNotNone(first_roi)
        self.assertIsNone(second_roi)
        self.assertEqual(anchor.raw_box, recovered.box)
        self.assertEqual(instance.player_track.misses, 0)

    def test_second_local_miss_reacquires_far_nameplate_after_teleport(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((20, 80, 20, 1), 0.9, "姓名板", (20, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.last_auxiliary_at = 10.0
        instance.player_track.misses = 1
        teleported = Detection(
            (340, 150, 20, 10),
            0.98,
            "player.png",
            identity_score=0.96,
        )
        instance._detect_player_nameplate.side_effect = [
            ([], -1.0, None),
            ([teleported], 0.98, "player.png"),
            ([teleported], 0.98, "player.png"),
        ]
        instance._detect_player_auxiliary.side_effect = [
            ([], -1.0, [], -1.0),
            ([], -1.0, [], -1.0),
            ([], -1.0, [], -1.0),
        ]
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        first = instance._track_player(scene, self.config["vision"], 10.1)
        anchor = instance._track_player(scene, self.config["vision"], 10.2)

        self.assertIsNone(first)
        self.assertEqual(anchor.raw_box, teleported.box)
        self.assertEqual(instance.player_track.misses, 0)

    def test_occluded_nameplate_uses_auxiliary_even_with_invalid_raw_candidate(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.misses = 1
        other = Detection(
            (105, 80, 20, 10),
            0.99,
            "other.png",
            identity_score=0.25,
        )
        instance._detect_player_nameplate.side_effect = [
            ([other], 0.99, "other.png"),
            ([other], 0.99, "other.png"),
        ]
        instance._detect_player_auxiliary.return_value = (
            [Detection((105, 46, 20, 20), 0.95, "other-head.png")],
            0.95,
            [Detection((105, 95, 20, 10), 0.95, "common-title.png")],
            0.95,
        )
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchor = instance._track_player(scene, self.config["vision"], 10.1)

        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.source, "头部")
        self.assertEqual(anchor.box[1], prior.box[1])
        self.assertEqual(instance.player_track.misses, 0)
        instance._detect_player_auxiliary.assert_called_once()

    def test_head_only_continues_tracking_after_identity_grace_and_periodic_global_scan(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        head = Detection((92, 46, 20, 20), 0.88, "own-head.png")
        instance._detect_player_nameplate.return_value = ([], -1.0, None)
        instance._detect_player_auxiliary.return_value = ([head], 0.88, [], -1.0)
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        sources = [
            instance._track_player(scene, self.config["vision"], now).source
            for now in (10.1, 10.5, 11.0, 11.6, 12.0)
        ]

        self.assertEqual(sources, ["头部"] * 5)
        self.assertEqual(instance.player_track.last_identity_at, 10.0)
        self.assertEqual(instance.player_track.misses, 0)
        self.assertIsNone(instance._detect_player_nameplate.call_args_list[3].args[2])

    def test_low_confidence_head_only_cannot_establish_identity_on_fresh_global_search(self):
        instance = self._tracker_bot()
        instance._detect_player_nameplate.return_value = ([], -1.0, None)
        instance._detect_player_auxiliary.return_value = (
            [Detection((92, 46, 20, 20), 0.89, "head.png")],
            0.89,
            [],
            -1.0,
        )
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchors = [instance._track_player(scene, self.config["vision"], 10.0 + index * 0.1) for index in range(4)]

        self.assertEqual(anchors, [None] * 4)
        self.assertFalse(instance.player_track.has_confirmed_identity())

    def test_consistent_high_confidence_head_can_establish_identity_after_three_frames(self):
        instance = self._tracker_bot()
        instance._detect_player_nameplate.return_value = ([], -1.0, None)
        head = Detection((92, 46, 20, 20), 0.96, "head.png")
        instance._detect_player_auxiliary.return_value = ([head], 0.96, [], -1.0)
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        first = instance._track_player(scene, self.config["vision"], 10.0)
        second = instance._track_player(scene, self.config["vision"], 10.1)
        third = instance._track_player(scene, self.config["vision"], 10.2)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIsNotNone(third)
        self.assertEqual(third.source, "头部")
        self.assertTrue(instance.player_track.has_confirmed_identity())
        self.assertEqual(instance.player_track.last_identity_at, 10.2)
        self.assertEqual(instance.player_track.misses, 0)

    def test_global_reacquisition_never_uses_head_or_title_without_identity(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((20, 80, 20, 1), 0.9, "姓名板", (20, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.misses = 2
        instance._detect_player_nameplate.return_value = ([], -1.0, None)
        instance._detect_player_auxiliary.return_value = (
            [Detection((300, 120, 20, 20), 0.99, "other-head.png")],
            0.99,
            [Detection((300, 170, 20, 10), 0.99, "common-title.png")],
            0.99,
        )
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchor = instance._track_player(scene, self.config["vision"], 10.2)

        self.assertIsNone(anchor)
        self.assertIs(instance.player_track.anchor, prior)

    def test_failed_global_fallback_hides_stale_anchor_from_actions(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.last_auxiliary_at = 10.0
        instance.player_track.misses = 1
        instance._detect_player_nameplate.side_effect = [([], -1.0, None), ([], -1.0, None)]
        instance._detect_player_auxiliary.side_effect = [
            ([], -1.0, [], -1.0),
            ([], -1.0, [], -1.0),
        ]
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchor = instance._track_player(scene, self.config["vision"], 10.1)

        self.assertIsNone(anchor)
        self.assertIs(instance.player_track.anchor, prior)
        self.assertEqual(instance.player_track.misses, 2)

    def test_later_global_frame_can_reacquire_far_nameplate_after_failed_fallback(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((20, 80, 20, 1), 0.9, "姓名板", (20, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.misses = 2
        teleported = Detection((340, 150, 20, 10), 0.98, "player.png", identity_score=0.96)
        instance._detect_player_nameplate.return_value = (
            [teleported],
            0.98,
            "player.png",
        )
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        first = instance._track_player(scene, self.config["vision"], 10.2)
        anchor = instance._track_player(scene, self.config["vision"], 10.3)

        self.assertIsNone(first)
        self.assertEqual(anchor.raw_box, teleported.box)
        self.assertEqual(instance.player_track.misses, 0)

    def test_known_nameplate_uses_local_low_threshold_recovery_after_hold_expires(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        weak_own_nameplate = Detection(
            (92, 80, 20, 10),
            0.68,
            "player.png",
            identity_score=0.96,
        )
        instance._detect_player_nameplate.side_effect = [
            ([], 0.68, "player.png"),
            ([weak_own_nameplate], 0.68, "player.png"),
            ([], 0.68, "player.png"),
            ([weak_own_nameplate], 0.68, "player.png"),
        ]
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        first = instance._track_player(scene, self.config["vision"], 11.0)
        anchor = instance._track_player(scene, self.config["vision"], 11.1)

        self.assertIsNone(first)
        self.assertIsNotNone(anchor)
        self.assertEqual(anchor.raw_box, weak_own_nameplate.box)
        self.assertEqual(anchor.source, "姓名板")
        self.assertIsNone(instance._detect_player_nameplate.call_args_list[0].args[2])
        recovery_call = instance._detect_player_nameplate.call_args_list[1]
        self.assertIsNotNone(recovery_call.args[2])
        self.assertEqual(recovery_call.kwargs["threshold"], 0.66)

    def test_low_threshold_recovery_rejects_candidate_far_from_known_player(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((20, 80, 20, 1), 0.9, "姓名板", (20, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        far_candidate = Detection(
            (340, 150, 20, 10),
            0.68,
            "player.png",
            identity_score=0.96,
        )
        instance._detect_player_nameplate.side_effect = [
            ([], 0.68, "player.png"),
            ([far_candidate], 0.68, "player.png"),
        ]
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchor = instance._track_player(scene, self.config["vision"], 11.0)

        self.assertIsNone(anchor)
        self.assertFalse(instance.nameplate_visible_this_frame)
        self.assertEqual(instance.player_track.misses, 1)

    def test_far_reacquisition_keeps_three_source_vote_against_wrong_nameplate(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((20, 80, 20, 1), 0.9, "姓名板", (20, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.misses = 2
        wrong = Detection((100, 80, 20, 10), 0.99, "wrong.png", identity_score=0.25)
        own_nameplate = Detection((300, 150, 20, 10), 0.90, "player.png", identity_score=0.96)
        own_head = Detection((300, 116, 20, 20), 0.88, "head.png")
        own_title = Detection((300, 165, 20, 10), 0.87, "title.png")
        instance._detect_player_nameplate.return_value = (
            [wrong, own_nameplate],
            0.99,
            "wrong.png",
        )
        instance._detect_player_auxiliary.return_value = (
            [own_head],
            0.88,
            [own_title],
            0.87,
        )
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        config = json.loads(json.dumps(self.config["vision"]))
        config["player_reacquire_confirm_frames"] = 1
        anchor = instance._track_player(scene, config, 10.2)

        self.assertEqual(anchor.raw_box, own_nameplate.box)
        self.assertEqual(anchor.source, "姓名板")

    def test_predicted_position_wins_over_candidate_at_old_location(self):
        instance = self._tracker_bot()
        first = PlayerAnchor((20, 80, 20, 1), 0.9, "姓名板", (20, 80, 20, 10))
        prior = PlayerAnchor((60, 80, 20, 1), 0.9, "姓名板", (60, 80, 20, 10))
        instance.player_track.record(first, 9.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.last_auxiliary_at = 10.0
        wrong_near_old = Detection((60, 80, 20, 10), 0.99, "other.png", identity_score=0.94)
        own_at_prediction = Detection((68, 80, 20, 10), 0.92, "player.png", identity_score=0.96)
        instance._detect_player_nameplate.return_value = (
            [wrong_near_old, own_at_prediction],
            0.99,
            "other.png",
        )
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        anchor = instance._track_player(scene, self.config["vision"], 10.2)

        self.assertEqual(anchor.raw_box, own_at_prediction.box)

    def test_periodic_verification_uses_full_scene_at_interval_boundary(self):
        instance = self._tracker_bot()
        prior = PlayerAnchor((90, 80, 20, 1), 0.9, "姓名板", (90, 80, 20, 10))
        instance.player_track.record(prior, 10.0, velocity_alpha=1.0, max_displacement=100.0)
        instance.player_track.last_global_at = 10.0
        instance.player_track.last_auxiliary_at = 10.0
        instance._detect_player_nameplate.return_value = (
            [Detection((90, 80, 20, 10), 0.95, "player.png", identity_score=0.96)],
            0.95,
            "player.png",
        )
        instance._detect_player_auxiliary.return_value = ([], -1.0, [], -1.0)
        scene = SceneFeatures(np.zeros((200, 400, 3), dtype=np.uint8))

        instance._track_player(scene, self.config["vision"], 11.5)

        self.assertIsNone(instance._detect_player_nameplate.call_args.args[2])
        instance._detect_player_auxiliary.assert_called_once()

    def test_old_config_receives_player_tracking_defaults(self):
        config = json.loads(json.dumps(self.config))
        for key in (
            "player_local_roi_width",
            "player_local_roi_up",
            "player_local_roi_down",
            "player_local_miss_limit",
            "player_global_verify_interval_seconds",
            "player_prediction_horizon_seconds",
            "player_velocity_alpha",
            "player_name_identity_threshold",
            "player_name_identity_margin",
            "player_reacquire_confirm_frames",
            "player_nameplate_recovery_threshold",
            "player_auxiliary_max_jump",
            "player_auxiliary_continuation_seconds",
            "player_auxiliary_identity_threshold",
            "player_auxiliary_reacquire_confirm_frames",
            "player_minimap_assist_enabled",
            "player_minimap_assist_max_seconds",
        ):
            config["vision"].pop(key, None)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_config(path)

        self.assertEqual(loaded["vision"]["player_local_miss_limit"], 2)
        self.assertEqual(loaded["vision"]["player_global_verify_interval_seconds"], 1.5)
        self.assertEqual(loaded["vision"]["player_local_roi_width"], 0.36)
        self.assertEqual(loaded["vision"]["player_reacquire_confirm_frames"], 2)
        self.assertEqual(loaded["vision"]["player_name_identity_threshold"], 0.50)
        self.assertEqual(loaded["vision"]["player_nameplate_recovery_threshold"], 0.66)
        self.assertEqual(loaded["vision"]["player_auxiliary_continuation_seconds"], 15.0)
        self.assertEqual(loaded["vision"]["player_auxiliary_identity_threshold"], 0.90)
        self.assertEqual(loaded["vision"]["player_auxiliary_reacquire_confirm_frames"], 3)
        self.assertTrue(loaded["vision"]["player_minimap_assist_enabled"])
        self.assertEqual(loaded["vision"]["player_minimap_assist_max_seconds"], 0.2)


class AutoPotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.behavior = {
            "hp_threshold": 0.35,
            "mp_threshold": 0.25,
            "potion_cooldown_seconds": 1.2,
        }
        self.keys = {"hp_potion": "home", "mp_potion": "end"}

    def test_hp_has_priority_and_hp_mp_have_independent_cooldowns(self):
        controller = AutoPotionController()
        hp_action = controller.decide(0.2, 0.1, 10.0, self.behavior, self.keys)
        self.assertEqual(hp_action.kind, "hp")
        controller.record(hp_action, 10.0)

        mp_action = controller.decide(0.2, 0.1, 10.1, self.behavior, self.keys)
        self.assertEqual(mp_action.kind, "mp")
        controller.record(mp_action, 10.1)
        self.assertIsNone(controller.decide(0.2, 0.1, 10.2, self.behavior, self.keys))

    def _bot(self) -> runtime_bot.BowmanBot:
        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.armed = False
        instance.input_authorized = True
        instance.integrity_ok = True
        instance.auto_potion = AutoPotionController()
        instance.auto_potion.set_enabled(True)
        instance.config = {"behavior": self.behavior, "keys": self.keys}
        instance.keyboard = MagicMock()
        instance.log = MagicMock()
        instance.stop_move = MagicMock()
        return instance

    def test_standalone_potion_works_while_paused_without_movement_action(self):
        instance = self._bot()
        window = WindowInfo(123, "MapleStory", 0, 0, 800, 600)
        with (
            patch.object(runtime_bot.user32, "IsWindow", return_value=True),
            patch.object(runtime_bot.user32, "IsIconic", return_value=False),
            patch.object(runtime_bot.user32, "GetForegroundWindow", return_value=123),
        ):
            used = instance._try_auto_potion(window, 0.2, 0.8, 10.0)

        self.assertTrue(used)
        instance.keyboard.tap.assert_called_once_with("home")
        instance.stop_move.assert_not_called()
        instance.log.write.assert_called_once_with("hp_potion", fill=0.2, standalone=True)

    def test_standalone_potion_never_sends_input_to_an_unfocused_game(self):
        instance = self._bot()
        window = WindowInfo(123, "MapleStory", 0, 0, 800, 600)
        with (
            patch.object(runtime_bot.user32, "IsWindow", return_value=True),
            patch.object(runtime_bot.user32, "IsIconic", return_value=False),
            patch.object(runtime_bot.user32, "GetForegroundWindow", return_value=999),
        ):
            used = instance._try_auto_potion(window, 0.2, 0.8, 10.0)

        self.assertFalse(used)
        self.assertTrue(instance.auto_potion.waiting_foreground)
        instance.keyboard.tap.assert_not_called()

    def test_global_potion_switch_blocks_armed_bot_when_off(self):
        instance = self._bot()
        instance.armed = True
        instance.auto_potion.set_enabled(False)
        window = WindowInfo(123, "MapleStory", 0, 0, 800, 600)
        with (
            patch.object(runtime_bot.user32, "IsWindow", return_value=True),
            patch.object(runtime_bot.user32, "IsIconic", return_value=False),
        ):
            used = instance._try_auto_potion(window, 0.2, 0.8, 10.0)

        self.assertFalse(used)
        instance.stop_move.assert_not_called()
        instance.keyboard.tap.assert_not_called()

    def test_global_potion_switch_allows_armed_bot_when_on(self):
        instance = self._bot()
        instance.armed = True
        window = WindowInfo(123, "MapleStory", 0, 0, 800, 600)
        with (
            patch.object(runtime_bot.user32, "IsWindow", return_value=True),
            patch.object(runtime_bot.user32, "IsIconic", return_value=False),
        ):
            used = instance._try_auto_potion(window, 0.2, 0.8, 10.0)

        self.assertTrue(used)
        instance.stop_move.assert_called_once_with()
        instance.keyboard.tap.assert_called_once_with("home")
        self.assertEqual(instance.state, "HP_POTION")

    def test_standalone_potion_safety_gates_block_input(self):
        window = WindowInfo(123, "MapleStory", 0, 0, 800, 600)
        cases = (
            ("未授权", False, True, True, False),
            ("完整性不足", True, False, True, False),
            ("窗口失效", True, True, False, False),
            ("窗口最小化", True, True, True, True),
        )
        for label, authorized, integrity, valid, iconic in cases:
            with self.subTest(label=label):
                instance = self._bot()
                instance.input_authorized = authorized
                instance.integrity_ok = integrity
                with (
                    patch.object(runtime_bot.user32, "IsWindow", return_value=valid),
                    patch.object(runtime_bot.user32, "IsIconic", return_value=iconic),
                ):
                    used = instance._try_auto_potion(window, 0.2, 0.8, 10.0)
                self.assertFalse(used)
                instance.keyboard.tap.assert_not_called()

    def test_keyboard_tap_releases_key_when_wait_is_interrupted(self):
        keyboard = Keyboard.__new__(Keyboard)
        keyboard._dispatch = MagicMock()
        code = vk_for("home")

        with patch("mbv.input.time.sleep", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                keyboard.tap("home")

        self.assertEqual(
            keyboard._dispatch.call_args_list,
            [call(code, False), call(code, True, was_down=True)],
        )

    def test_keyboard_tap_attempts_keyup_when_keydown_partially_fails(self):
        keyboard = Keyboard.__new__(Keyboard)
        keyboard._dispatch = MagicMock(side_effect=[RuntimeError("partial down"), None])
        code = vk_for("home")

        with self.assertRaisesRegex(RuntimeError, "partial down"):
            keyboard.tap("home")

        self.assertEqual(
            keyboard._dispatch.call_args_list,
            [call(code, False), call(code, True, was_down=True)],
        )

    def test_latest_global_potion_request_wins_and_suspended_mode_cannot_enable(self):
        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.action_lock = threading.RLock()
        instance.potion_enabled_requested = None
        instance.request_auto_potion(True)
        instance.request_auto_potion(False)
        self.assertFalse(instance.potion_enabled_requested)

        instance.auto_potion = AutoPotionController()
        instance.vision_suspended = threading.Event()
        instance.vision_suspended.set()
        instance.notify = MagicMock()
        instance._set_auto_potion(WindowInfo(123, "MapleStory", 0, 0, 800, 600), True)
        self.assertFalse(instance.auto_potion.enabled)


class BuffConfigTests(unittest.TestCase):
    def test_old_config_receives_three_disabled_buff_slots(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        config.pop("buffs", None)
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_config(path)

        self.assertEqual(
            loaded["buffs"],
            {
                "buff_1": {"enabled": False, "key": "", "interval_seconds": 0.0},
                "buff_2": {"enabled": False, "key": "", "interval_seconds": 0.0},
                "buff_3": {"enabled": False, "key": "", "interval_seconds": 0.0},
            },
        )

    def test_buff_intervals_are_normalized_and_bounded(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        config["buffs"] = {
            "buff_1": {"key": " A ", "interval_seconds": "30"},
            "buff_2": {"key": "B", "interval_seconds": -5},
            "buff_3": {"key": "C", "interval_seconds": float("inf")},
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded = load_config(path)

        self.assertEqual(
            loaded["buffs"]["buff_1"],
            {"enabled": True, "key": "a", "interval_seconds": 30.0},
        )
        self.assertFalse(loaded["buffs"]["buff_2"]["enabled"])
        self.assertEqual(loaded["buffs"]["buff_2"]["interval_seconds"], 0.0)
        self.assertFalse(loaded["buffs"]["buff_3"]["enabled"])
        self.assertEqual(loaded["buffs"]["buff_3"]["interval_seconds"], 0.0)


class AutoBuffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.buffs = {
            "buff_1": {"key": "a", "interval_seconds": 10.0},
            "buff_2": {"key": "b", "interval_seconds": 20.0},
            "buff_3": {"key": "", "interval_seconds": 30.0},
        }

    def test_three_slots_cast_immediately_then_keep_independent_intervals(self):
        controller = AutoBuffController()

        first = controller.decide(self.buffs, 100.0)
        self.assertEqual((first.slot, first.key), ("buff_1", "a"))
        controller.record(first, 100.0)
        self.assertIsNone(controller.decide(self.buffs, 100.99))

        second = controller.decide(self.buffs, 101.2)
        self.assertEqual((second.slot, second.key), ("buff_2", "b"))
        controller.record(second, 101.2)
        self.assertIsNone(controller.decide(self.buffs, 109.99))
        self.assertEqual(controller.decide(self.buffs, 110.0).slot, "buff_1")

    def test_zero_interval_or_blank_key_disables_slot(self):
        controller = AutoBuffController()
        disabled = {
            "buff_1": {"key": "a", "interval_seconds": 0.0},
            "buff_2": {"key": "", "interval_seconds": 10.0},
            "buff_3": {"key": "c", "interval_seconds": -1.0},
        }
        self.assertIsNone(controller.decide(disabled, 100.0))

    def test_explicit_switch_disables_only_selected_slot_and_preserves_timer(self):
        controller = AutoBuffController()
        first = controller.decide(self.buffs, 100.0)
        controller.record(first, 100.0)
        selected = {
            "buff_1": {"enabled": False, "key": "a", "interval_seconds": 10.0},
            "buff_2": {"enabled": True, "key": "b", "interval_seconds": 20.0},
            "buff_3": {"enabled": False, "key": "c", "interval_seconds": 30.0},
        }

        second = controller.decide(selected, 101.2)
        self.assertEqual(second.slot, "buff_2")
        controller.record(second, 101.2)
        self.assertIsNone(controller.decide(selected, 110.0))

        selected["buff_1"]["enabled"] = True
        self.assertEqual(controller.decide(selected, 110.0).slot, "buff_1")

    def test_bot_only_casts_buff_while_armed(self):
        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.armed = True
        instance.input_authorized = True
        instance.auto_buff = AutoBuffController()
        instance.config = {"buffs": self.buffs}
        instance.keyboard = MagicMock()
        instance.log = MagicMock()
        instance.stop_move = MagicMock()
        instance._reset_player_lost_recovery = MagicMock()

        instance.window = MagicMock(hwnd=123)
        instance.background_input = True
        with (
            patch("mbv.bot.time.sleep") as sleep,
            patch("mbv.bot.time.monotonic", return_value=10.45),
            patch("mbv.bot.user32.IsWindow", return_value=True),
            patch("mbv.bot.user32.IsIconic", return_value=False),
        ):
            self.assertTrue(instance._try_auto_buff(10.0))
            instance.keyboard.tap.assert_not_called()
            self.assertTrue(instance._try_auto_buff(10.45))
        sleep.assert_not_called()
        instance.keyboard.tap.assert_called_once_with("a", BUFF_KEY_HOLD_SECONDS)
        self.assertEqual(instance.stop_move.call_count, 2)
        self.assertEqual(instance.state, "BUFF_1")
        instance.log.write.assert_called_once_with(
            "buff",
            slot="buff_1",
            key="a",
            interval_seconds=10.0,
            pre_cast_seconds=BUFF_PRE_CAST_SECONDS,
            key_hold_seconds=BUFF_KEY_HOLD_SECONDS,
            result="key_sent_unverified",
        )

        instance.keyboard.reset_mock()
        instance.stop_move.reset_mock()
        self.assertTrue(instance._try_auto_buff(10.5))
        instance.keyboard.tap.assert_not_called()
        instance.stop_move.assert_called_once_with()

        instance.armed = False
        instance.keyboard.reset_mock()
        self.assertFalse(instance._try_auto_buff(30.0))
        instance.keyboard.tap.assert_not_called()

    def test_pause_preserves_last_buff_cast_time(self):
        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.armed = True
        instance.state = "BUFF_1"
        instance.keyboard = MagicMock()
        instance.window = None
        instance.window_topmost = False
        instance.log = MagicMock()
        instance.notify = MagicMock()
        instance.auto_buff = AutoBuffController()
        only_one = {
            "buff_1": {"key": "a", "interval_seconds": 120.0},
            "buff_2": {"key": "", "interval_seconds": 0.0},
            "buff_3": {"key": "", "interval_seconds": 0.0},
        }
        action = instance.auto_buff.decide(only_one, 100.0)
        instance.auto_buff.record(action, 100.0)

        with patch.object(runtime_bot.user32, "MessageBeep"):
            instance.disarm("测试暂停")

        self.assertIsNone(instance.auto_buff.decide(only_one, 150.0))
        self.assertIsNotNone(instance.auto_buff.decide(only_one, 220.0))


class PanelPotionTests(unittest.TestCase):
    def test_individual_buff_switch_persists_and_updates_runtime_without_disarming(self):
        from mbv.panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.busy = False
        panel.root = MagicMock()
        panel.config_path = Path("config.json")
        panel.bot = MagicMock()
        variable = MagicMock()
        variable.get.return_value = False
        panel.buff_enabled = {"buff_2": variable}
        panel._buff_toggle_buttons = {}
        config = {
            "buffs": {
                "buff_2": {"enabled": True, "key": "delete", "interval_seconds": 120.0}
            }
        }

        with patch("mbv.panel.load_config", return_value=config), patch(
            "mbv.panel.save_config"
        ) as save:
            panel._toggle_buff_slot("buff_2")

        self.assertFalse(config["buffs"]["buff_2"]["enabled"])
        save.assert_called_once_with(panel.config_path, config)
        panel.bot.preview_config_setting.assert_called_once_with(
            "buffs.buff_2.enabled",
            False,
        )
        panel.bot.notify.assert_called_once_with("Buff 2 已关闭", 3.0)

    def test_buff_interval_direct_input_updates_and_persists(self):
        from mbv.panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.root = MagicMock()
        entry = MagicMock()
        entry.get.return_value = "30"
        persist = MagicMock()

        with patch("mbv.panel.simpledialog.askfloat", return_value=123.5) as askfloat:
            panel._prompt_numeric_entry(entry, "Buff 1 间隔秒", 0.0, 86400.0, persist)

        self.assertEqual(askfloat.call_args.kwargs["initialvalue"], 30.0)
        self.assertEqual(askfloat.call_args.kwargs["minvalue"], 0.0)
        self.assertEqual(askfloat.call_args.kwargs["maxvalue"], 86400.0)
        persist.assert_called_once_with("123.5")
        entry.delete.assert_called_once_with(0, "end")
        entry.insert.assert_called_once_with(0, "123.5")

    def test_panel_potion_button_queues_global_toggle(self):
        from mbv.panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.busy = False
        panel.bot = MagicMock()
        panel.auto_potion_enabled = MagicMock()

        panel.auto_potion_enabled.get.return_value = True

        panel._toggle_auto_potion()

        panel.bot.request_auto_potion.assert_called_once_with(True)

    def test_all_comboboxes_can_block_mousewheel_changes(self):
        from mbv.panel import disable_combobox_mousewheel

        combo = MagicMock()
        disable_combobox_mousewheel(combo)

        self.assertEqual(
            [item.args[0] for item in combo.bind.call_args_list],
            ["<MouseWheel>", "<Button-4>", "<Button-5>"],
        )
        self.assertTrue(all(item.kwargs.get("add") == "+" for item in combo.bind.call_args_list))
        self.assertTrue(all(item.args[1](None) == "break" for item in combo.bind.call_args_list))


if __name__ == "__main__":
    unittest.main()
