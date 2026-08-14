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
            "player_auxiliary_max_jump",
            "player_auxiliary_identity_threshold",
            "player_auxiliary_reacquire_confirm_frames",
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
        self.assertEqual(loaded["vision"]["player_auxiliary_identity_threshold"], 0.90)
        self.assertEqual(loaded["vision"]["player_auxiliary_reacquire_confirm_frames"], 3)


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
        instance.auto_potion.set_standalone_enabled(True)
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

    def test_armed_bot_keeps_original_potion_priority_when_standalone_is_off(self):
        instance = self._bot()
        instance.armed = True
        instance.auto_potion.set_standalone_enabled(False)
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

    def test_latest_potion_mode_request_wins_and_suspended_mode_cannot_enable(self):
        instance = runtime_bot.BowmanBot.__new__(runtime_bot.BowmanBot)
        instance.action_lock = threading.RLock()
        instance.potion_mode_requested = None
        instance.request_standalone_potion(True)
        instance.request_standalone_potion(False)
        self.assertFalse(instance.potion_mode_requested)

        instance.auto_potion = AutoPotionController()
        instance.vision_suspended = threading.Event()
        instance.vision_suspended.set()
        instance.notify = MagicMock()
        instance._set_standalone_potion(WindowInfo(123, "MapleStory", 0, 0, 800, 600), True)
        self.assertFalse(instance.auto_potion.standalone_enabled)


class PanelPotionTests(unittest.TestCase):
    def test_panel_potion_button_only_queues_session_toggle(self):
        from mbv.panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.busy = False
        panel.bot = MagicMock()
        panel.standalone_potion = MagicMock()

        panel.standalone_potion.get.return_value = True

        panel._toggle_standalone_potion()

        panel.bot.request_standalone_potion.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
