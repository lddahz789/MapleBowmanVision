from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from mbv.bot import BowmanBot
from mbv.localization_diagnostics import LocalizationDiagnostics
from mbv.player_tracking import PlayerTrackState
from mbv.vision import Detection, SceneFeatures, Template, find_detections


class LocalizationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.observer = LocalizationDiagnostics(Path(self.temp.name) / "session-test.jsonl")
        self.addCleanup(self.observer.close)
        self.frame = np.full((80, 120, 3), 42, np.uint8)
        self.minimap = np.full((12, 16, 3), 84, np.uint8)

    def observe(self, now, visual=False, armed=True):
        self.observer.observe(self.frame, self.minimap,
                              {"state": "MINIMAP_WAITING_VISUAL", "reason": "no_template_candidate"},
                              now, visual_ok=visual, armed=armed)

    def records(self):
        self.observer.close()
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(self.observer.directory.glob("*.json"))]

    def test_short_loss_does_not_write_or_start_thread(self):
        self.observe(10, visual=True)
        self.observe(11)
        self.observe(12, visual=True)
        self.assertIsNone(self.observer._thread)
        self.assertFalse(self.observer.directory.exists())

    def test_original_before_loss_and_recovery_frames_are_saved(self):
        self.observe(10, visual=True)
        self.frame[:] = 43
        self.observe(11)
        self.observe(13)
        self.observe(14, visual=True)
        records = self.records()
        self.assertEqual([r["phase"] for r in records], ["before_loss", "lost", "recovered"])
        self.assertEqual(records[-1]["lost_seconds"], 3)
        self.assertTrue(all(r["image_status"] == "saved" for r in records))
        pixels = cv2.imdecode(np.fromfile(self.observer.directory / "0001-combat.png", np.uint8), 1)
        self.assertTrue(np.all(pixels == 42))
        self.assertEqual(records[1]["recent_frames"][-1]["monotonic"], 13)
        self.assertEqual(records[1]["first_failure"]["monotonic"], 11)

    def test_pause_ends_episode_and_does_not_count_paused_time(self):
        self.observe(10)
        self.observe(12)
        self.observe(13, armed=False)
        self.observe(40)
        self.observe(42)
        self.observe(43, visual=True)
        records = self.records()
        self.assertEqual([r["phase"] for r in records], ["lost", "interrupted", "lost", "recovered"])
        self.assertFalse(records[1]["armed"])
        self.assertEqual(records[-1]["lost_seconds"], 3)
        self.assertEqual(records[-1]["episode"], 2)

    def test_rate_limit_and_session_snapshot_cap(self):
        self.observer.max_snapshots = 1
        for now in (10, 12, 13, 14, 17, 18, 22, 27):
            self.observe(now)
            if self.observer._thread:
                self.observer._queue.join()
        records = self.records()
        self.assertEqual([r["monotonic"] for r in records], [12, 17, 22, 27])
        self.assertEqual(len(list(self.observer.directory.glob("*-combat.png"))), 1)
        self.assertEqual(records[1]["image_status"], "snapshot_limit")
        self.assertEqual(records[-1]["image_status"], "not_scheduled")

    def test_global_storage_budget_disables_only_diagnostics(self):
        self.observer.max_bytes = 1
        self.observe(10)
        self.observe(12)
        self.observer.close()
        self.assertFalse(self.observer.enabled)
        self.assertIn("上限", self.observer.take_notice())
        self.assertFalse(list(self.observer.directory.glob("*.json")))

    def test_encode_error_isolated_and_reported(self):
        with patch("mbv.localization_diagnostics.cv2.imencode", side_effect=OSError("disk failure")):
            self.observe(10)
            self.observe(12)
            self.observer.close()
        self.assertIn("disk failure", self.observer.take_notice())
        self.assertIsNone(self.observer.take_notice())
        self.assertFalse(list(self.observer.directory.glob("*.json")))

    def test_full_queue_drops_without_blocking_and_metadata_is_frozen(self):
        with patch("mbv.localization_diagnostics.threading.Thread"):
            self.observe(10)
            for now in (12, 17, 22, 27, 32):
                self.observe(now)
            self.assertEqual(self.observer._queue.qsize(), 4)
            self.assertEqual(self.observer._dropped, 1)
            self.assertTrue(self.observer.enabled)
            _, payload, images = self.observer._queue.get_nowait()
            self.observer._queue.task_done()
            self.frame[:] = 99
            self.assertTrue(np.all(images[0] == 42))
            self.assertEqual(json.loads(payload)["monotonic"], 12)

    def test_sample_unchanged_is_evidence_not_a_capture_failure(self):
        self.observe(10)
        self.observe(12)
        records = self.records()
        self.assertEqual(records[0]["sample_unchanged_seconds"], 2)
        self.assertEqual(records[0]["reason"], "no_template_candidate")

    def test_oversized_images_still_produce_text(self):
        self.frame = np.zeros((2400, 2400, 3), np.uint8)
        self.observe(10)
        self.observe(12)
        self.assertEqual(self.records()[0]["image_status"], "frame_too_large")

    def test_bot_records_post_action_state_and_capture_source(self):
        bot = BowmanBot.__new__(BowmanBot)
        bot.localization_diagnostics = self.observer
        bot.log = MagicMock()
        bot.notify = MagicMock()
        bot.armed, bot.delivery, bot.state = True, "hybrid", "MINIMAP_VISUAL_TIMEOUT"
        bot._localization_frame_diagnostic = {"visual_ok": False, "reason": "no_template_candidate"}
        for now in (10, 12):
            bot._observe_localization_diagnostics(SceneFeatures(self.frame), self.minimap, now,
                combat_rect=(0, 0, 120, 80), minimap_rect=(0, 0, 16, 12),
                capture_foreground=False, marker_candidates=1)
        record = self.records()[0]
        self.assertEqual(record["state"], "MINIMAP_VISUAL_TIMEOUT")
        self.assertEqual(record["capture_source"], "PrintWindow")
        self.assertFalse(record["capture_foreground"])
        self.assertEqual(record["marker_candidate_count"], 1)


class MatchDiagnosticsTests(unittest.TestCase):
    def test_tracking_sequence_identical_with_diagnostics_enabled(self):
        plate = np.full((24, 64, 3), (180, 70, 20), np.uint8)
        cv2.putText(plate, "AB", (18, 17), cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1)
        template = Template("own.png", plate, np.full((24, 64), 255, np.uint8))
        bots = []
        for enabled in (False, True):
            bot = BowmanBot.__new__(BowmanBot)
            bot.player_track = PlayerTrackState()
            bot.player_templates = [template]
            bot.player_head_templates = []
            bot.player_title_templates = []
            bot.log = MagicMock()
            bot.localization_diagnostics = MagicMock(enabled=True) if enabled else None
            bots.append(bot)
        for i, position in enumerate((40, 40, None, None, None, 41, 41, 41)):
            pixels = np.zeros((120, 180, 3), np.uint8)
            if position is not None:
                pixels[40:64, position:position + 64] = plate
            outputs = [bot._track_player(SceneFeatures(pixels.copy()),
                {"player_detection_scale": .5, "player_template_threshold": .7},
                10 + i, marker=(.5, .5), marker_unambiguous=True, marker_size=(100, 100))
                for bot in bots]
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(bots[0].player_track.pending_count, bots[1].player_track.pending_count)
            self.assertEqual(bots[0].player_track.last_seen_at, bots[1].player_track.last_seen_at)

    def test_below_threshold_peak_and_roi_coordinates_without_extra_matching(self):
        rng = np.random.default_rng(9)
        pixels = rng.integers(0, 255, (120, 180, 3), dtype=np.uint8)
        template = Template("own.png", pixels[41:65, 41:105].copy())
        scene = SceneFeatures(pixels)
        normal = find_detections(scene, [template], 1.1, search_roi=(21, 21, 110, 80))
        scene.match_diagnostics = []
        with patch("mbv.vision.cv2.matchTemplate", wraps=cv2.matchTemplate) as match:
            observed = find_detections(scene, [template], 1.1, search_roi=(21, 21, 110, 80))
        self.assertEqual(observed, normal)
        self.assertEqual(match.call_count, 1)
        peak = scene.match_diagnostics[0]["best_matches"][0]
        self.assertEqual(peak["box"], [41, 41, 64, 24])
        self.assertFalse(peak["passed_threshold"])

    def test_identity_rejection_trace_keeps_tracker_decision(self):
        bot = BowmanBot.__new__(BowmanBot)
        bot.log = MagicMock()
        bot.localization_diagnostics = MagicMock(enabled=True)
        bot.player_track = PlayerTrackState()
        pixels = np.random.default_rng(8).integers(0, 255, (120, 180, 3), dtype=np.uint8)
        bot.player_templates = [Template("own.png", pixels[40:64, 40:104].copy())]
        bot._detect_player_auxiliary = MagicMock(return_value=([], -1., [], -1.))
        rejected = Detection((40, 40, 64, 24), .99, "own.png", identity_score=.1)
        scene = SceneFeatures(pixels)
        with patch("mbv.bot.verify_nameplate_identities", return_value=[rejected]):
            result = bot._track_player(scene, {"player_detection_scale": 1.0}, 10)
        self.assertIsNone(result)
        diagnostic = bot._localization_frame_diagnostic
        self.assertEqual(diagnostic["reason"], "nameplate_identity_or_recovery_distance_rejected")
        self.assertFalse(diagnostic["passes"][0]["identity_candidates"][0]["passed_identity"])
        self.assertEqual(diagnostic["after"]["pending"]["count"], 0)
        self.assertIsNone(scene.match_diagnostics)
        self.assertFalse(bot.player_track.has_nameplate_identity())


if __name__ == "__main__":
    unittest.main()
