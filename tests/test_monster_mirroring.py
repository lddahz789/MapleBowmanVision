from dataclasses import replace
import unittest

import cv2
import numpy as np

from mbv.vision import Template, SceneFeatures, find_detections, suppress_monster_detections, Detection


class MonsterMirroringTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(12)
        self.pixels = rng.integers(20, 235, (24, 32, 3), dtype=np.uint8)
        self.mask = np.full((24, 32), 255, np.uint8)
        self.mask[:7, :9] = 0
        self.template = Template("怪猫/sample.png", self.pixels.copy(), self.mask.copy(), (4., 20.))
        self.scene = rng.integers(20, 235, (100, 200, 3), dtype=np.uint8)
        self.scene[10:34, 10:42] = self.pixels
        self.scene[50:74, 120:152] = cv2.flip(self.pixels, 1)

    def test_default_unchanged_and_opt_in_detects_both_directions(self):
        default, _, _ = find_detections(self.scene, [self.template], .98)
        self.assertEqual([d.box for d in default], [(10, 10, 32, 24)])
        both, _, _ = find_detections(self.scene, [self.template], .98, mirror_horizontal=True)
        self.assertEqual({d.box for d in both}, {(10, 10, 32, 24), (120, 50, 32, 24)})
        self.assertEqual({d.name for d in both}, {self.template.name})

    def test_mirror_alpha_anchor_and_cache_preserve_original(self):
        mirrored = self.template.mirrored()
        self.assertIs(mirrored, self.template.mirrored())
        self.assertTrue(np.array_equal(mirrored.image, cv2.flip(self.pixels, 1)))
        self.assertTrue(np.array_equal(mirrored.foreground_mask, cv2.flip(self.mask, 1)))
        self.assertEqual(mirrored.anchor_offset, (27., 20.))
        self.assertTrue(np.array_equal(self.template.image, self.pixels))
        self.assertTrue(np.array_equal(self.template.foreground_mask, self.mask))
        first = mirrored.scaled_features(.5)
        self.assertIs(first, self.template.mirrored().scaled_features(.5))

    def test_cross_template_and_mirror_duplicates_count_once(self):
        pixels = np.concatenate((self.pixels[:, :16], self.pixels[:, :16][:, ::-1]), axis=1)
        template = Template("怪猫/symmetric.png", pixels, np.full((24, 32), 255, np.uint8))
        scene = self.scene.copy()
        scene[10:34, 10:42] = pixels
        results, _, _ = find_detections(scene, [template, replace(template, name="怪猫/duplicate.png")],
                                        .98, mirror_horizontal=True)
        self.assertEqual(len(results), 1)

    def test_scaled_roi_and_filter_keep_source_coordinates_and_category(self):
        results, _, _ = find_detections(SceneFeatures(self.scene), [self.template], .9, .5,
                                        search_roi=(100, 40, 80, 50), mirror_horizontal=True)
        self.assertEqual([d.box for d in results], [(120, 50, 32, 24)])
        filtered = suppress_monster_detections(results, [Detection((120, 50, 32, 24), 1., "怪猫/filter.png")], .5)
        self.assertEqual(filtered, [])

    def test_unmatched_background_does_not_gain_detections(self):
        blank = np.full_like(self.scene, 80)
        results, _, _ = find_detections(blank, [self.template], .98, mirror_horizontal=True)
        self.assertEqual(results, [])
