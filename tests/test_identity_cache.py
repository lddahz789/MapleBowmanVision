from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv.vision import (  # noqa: E402
    Detection,
    Template,
    nameplate_identity_mask,
    nameplate_identity_similarity,
    verify_nameplate_identities,
)


def reference_similarity(
    template: np.ndarray,
    candidate: np.ndarray,
    foreground_mask: np.ndarray | None,
) -> float:
    """独立的补边/切片参考实现，检查缓存前后仍为原九偏移 Dice 分数。"""
    if template.size == 0 or candidate.size == 0:
        return 0.0
    height, width = template.shape[:2]
    if candidate.shape[:2] != (height, width):
        candidate = cv2.resize(candidate, (width, height), interpolation=cv2.INTER_AREA)
    expected = nameplate_identity_mask(template, foreground_mask) > 0
    actual = nameplate_identity_mask(candidate, foreground_mask) > 0
    expected_count = int(np.count_nonzero(expected))
    if expected_count < 3 or int(np.count_nonzero(actual)) < 3:
        return 0.0
    padded = np.pad(actual, 1, constant_values=False)
    scores = []
    for top in range(3):
        for left in range(3):
            shifted = padded[top:top + height, left:left + width]
            intersection = int(np.count_nonzero(expected & shifted))
            scores.append(2.0 * intersection / max(1, expected_count + int(np.count_nonzero(shifted))))
    return max(scores)


def synthetic_nameplate(text: str = "AB") -> np.ndarray:
    image = np.full((24, 64, 3), (180, 70, 20), dtype=np.uint8)
    cv2.putText(image, text, (18, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (245, 245, 245), 1)
    return image


class NameplateIdentityCacheTests(unittest.TestCase):
    def test_cached_verification_matches_reference_for_deterministic_matrix(self):
        rng = np.random.default_rng(20260912)
        for height, width in ((1, 1), (2, 3), (8, 7), (24, 64), (26, 96)):
            image = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
            image[height // 3:height * 2 // 3, width // 3:width * 2 // 3] = 235
            candidates = [
                image.copy(),
                np.roll(image, (1, -1), axis=(0, 1)),
                np.full_like(image, (180, 70, 20)),
                rng.integers(0, 256, image.shape, dtype=np.uint8),
            ]
            masks = [
                None,
                np.full((height, width), 255, dtype=np.uint8),
                np.zeros((height, width), dtype=np.uint8),
                rng.choice(np.array([0, 255], dtype=np.uint8), (height, width)),
                np.full((max(1, height // 2), max(1, width // 2)), 255, dtype=np.uint8),
            ]
            for mask_index, mask in enumerate(masks):
                template = Template("synthetic.png", image, mask, (4.0, 7.0))
                detection = Detection((0, 0, width, height), 0.92, template.name)
                for candidate_index, candidate in enumerate(candidates):
                    with self.subTest(shape=(height, width), mask=mask_index, candidate=candidate_index):
                        expected = reference_similarity(image, candidate, mask)
                        verified = verify_nameplate_identities(candidate, [detection], [template])[0]
                        self.assertEqual(verified.identity_score, expected)
                        self.assertEqual(nameplate_identity_similarity(image, candidate, mask), expected)
                        self.assertEqual(verified.box, detection.box)
                        self.assertEqual(verified.score, detection.score)
                        self.assertEqual(verified.anchor_offset, (4.0, 7.0))

    def test_standalone_similarity_keeps_resize_and_empty_behavior(self):
        image = synthetic_nameplate()
        mask = np.full(image.shape[:2], 255, dtype=np.uint8)
        for candidate in (
            cv2.resize(image, (32, 12), interpolation=cv2.INTER_AREA),
            cv2.resize(image, (128, 48), interpolation=cv2.INTER_NEAREST),
            np.empty((0, 0, 3), dtype=np.uint8),
        ):
            with self.subTest(shape=candidate.shape):
                self.assertEqual(
                    nameplate_identity_similarity(image, candidate, mask),
                    reference_similarity(image, candidate, mask),
                )
        self.assertEqual(nameplate_identity_similarity(np.empty((0, 0, 3), dtype=np.uint8), image), 0.0)

    def test_template_extraction_runs_once_but_candidates_are_fresh_every_frame(self):
        image = synthetic_nameplate()
        template = Template("player.png", image)
        detection = Detection((0, 0, 64, 24), 0.9, template.name)
        scores = []
        with patch("mbv.vision.nameplate_identity_mask", wraps=nameplate_identity_mask) as extract:
            for scene in (image.copy(), synthetic_nameplate("XY"), image.copy()):
                verified = verify_nameplate_identities(scene, [detection, detection], [template])
                scores.append(verified[0].identity_score)
            template_calls = sum(call.args[0] is image for call in extract.call_args_list)
        self.assertEqual(template_calls, 1)
        self.assertEqual(extract.call_count, 7)
        self.assertEqual(scores[0], 1.0)
        self.assertLess(scores[1], scores[0] - 0.25)
        self.assertEqual(scores[2], 1.0)

    def test_transparent_white_border_keeps_original_alpha_threshold(self):
        image = np.full((26, 64, 3), 255, dtype=np.uint8)
        image[3:23, 10:54] = (180, 70, 20)
        cv2.putText(image, "AB", (18, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (230, 230, 230), 1)
        alpha = np.zeros((26, 64), dtype=np.uint8)
        alpha[3:23, 10:54] = 255
        candidate = image.copy()
        candidate[alpha == 0] = 0
        template = Template("alpha.png", image, alpha)
        detection = Detection((0, 0, 64, 26), 0.9, template.name)
        score = verify_nameplate_identities(candidate, [detection], [template])[0].identity_score
        self.assertEqual(score, reference_similarity(image, candidate, alpha))
        self.assertGreater(score, 0.9)

    def test_reloaded_template_with_same_name_does_not_share_cache(self):
        original = Template("player.png", synthetic_nameplate())
        original_features = original.nameplate_identity_features()
        reloaded = replace(original, image=synthetic_nameplate("XY"))
        detection = Detection((0, 0, 64, 24), 0.9, reloaded.name)
        score = verify_nameplate_identities(reloaded.image, [detection], [reloaded])[0].identity_score
        self.assertEqual(score, 1.0)
        self.assertIsNot(reloaded.nameplate_identity_features(), original_features)
        self.assertFalse(np.array_equal(reloaded.nameplate_identity_features()[0], original_features[0]))

    def test_cache_is_lazy_readonly_and_does_not_change_input_arrays(self):
        image = synthetic_nameplate()
        alpha = np.full(image.shape[:2], 255, dtype=np.uint8)
        original_image, original_alpha = image.copy(), alpha.copy()
        template = Template("player.png", image, alpha)
        self.assertIsNone(template._identity_cache)
        features = template.nameplate_identity_features()
        self.assertIs(features, template.nameplate_identity_features())
        self.assertFalse(features[0].flags.writeable)
        self.assertTrue(image.flags.writeable)
        self.assertTrue(alpha.flags.writeable)
        np.testing.assert_array_equal(image, original_image)
        np.testing.assert_array_equal(alpha, original_alpha)

    def test_missing_template_and_clipped_detection_still_fail_closed(self):
        image = synthetic_nameplate()
        template = Template("player.png", image)
        detections = [
            Detection((0, 0, 64, 24), 0.99, "missing.png"),
            Detection((-1, 0, 64, 24), 0.99, template.name),
            Detection((0, 0, 32, 24), 0.99, template.name),
        ]
        verified = verify_nameplate_identities(image, detections, [template])
        self.assertEqual([item.identity_score for item in verified], [0.0, 0.0, 0.0])
        self.assertIsNone(template._identity_cache)


if __name__ == "__main__":
    unittest.main()
