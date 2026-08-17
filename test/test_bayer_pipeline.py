#!/usr/bin/env python3
"""bayer_pipeline.py 取证管线数学验证 (test/test_bayer_pipeline.py)

对 `bayer_pipeline.py` 的纯函数层做确定性验证, 锁定粉区缺陷修复行为:
  g_map                  : 位序解码恒等 (b1=MSB, 序 (1,7,6,5,4,3))
  fix_defect_region      : 楔形区域修复 — 带外同相位参考, 差>阈替换,
                           缺陷区掩码/计数与最终交付版 final_v8 一致
  fix_dead_pixels        : 散点死点修复 — 跳过区域修复掩码, 只修孤点
  render_stitched        : 上/下分相位 + 灰度世界 WB + 伽马, 尺寸/通道序正确

运行: python3 -m unittest test.test_bayer_pipeline -v
"""

import os
import sys
import unittest

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bayer_pipeline as bp  # noqa: E402  真实管线实现

_REGION_COLS = range(502, 517)
_REGION_Y = (60, 160)
_REGION_THRESH = 55.0
_BG_VAL = 2
_DEFECT_EVEN = 146
_DEFECT_ODD = 80


def _make_synthetic_raw():
    raw = np.full((480, 640), _BG_VAL, dtype=np.uint8)
    for y in range(_REGION_Y[0], _REGION_Y[1]):
        for x in _REGION_COLS:
            raw[y, x] = _DEFECT_EVEN if x % 2 == 0 else _DEFECT_ODD
    dead = [(200, 100, 0), (200, 200, 0)]
    for dy, dx, val in dead:
        raw[dy, dx] = val
    return raw


_SYNTH_RAW = _make_synthetic_raw()


class TestGMap(unittest.TestCase):

    def test_b1_msb(self):
        self.assertEqual(bp.g_map(np.array([2], np.uint8))[0], 128)
        self.assertEqual(bp.g_map(np.array([128], np.uint8))[0], 64)
        self.assertEqual(bp.g_map(np.array([254], np.uint8))[0], 252)

    def test_g_map_is_four_multiple(self):
        v = np.arange(256, dtype=np.uint8)
        g = bp.g_map(v)
        self.assertTrue((g % 4 == 0).all())
        self.assertTrue((g <= 252).all())


class TestFixDefectRegion(unittest.TestCase):

    def test_synthetic_defect(self):
        cfa = np.full((8, 8), 100.0)
        cfa[:, 4] = 250.0
        fixed, mask, n = bp.fix_defect_region(cfa, {4}, 0, 8, threshold=55.0)
        self.assertEqual(n, 8)
        self.assertEqual(mask.sum(), 8)
        np.testing.assert_allclose(fixed[:, 4], 100.0)

    def test_region_threshold_respects_inner(self):
        cfa = np.full((6, 6), 100.0)
        cfa[:, 3] = 130.0
        fixed, mask, n = bp.fix_defect_region(cfa, {3}, 0, 6, threshold=55.0)
        self.assertEqual(n, 0)
        self.assertEqual(mask.sum(), 0)
        np.testing.assert_allclose(fixed, cfa)

    def test_synthetic_frame_replacement_count(self):
        cfa = bp.g_map(_SYNTH_RAW).astype(np.float64)
        fixed, mask, n = bp.fix_defect_region(
            cfa, _REGION_COLS, _REGION_Y[0], _REGION_Y[1], _REGION_THRESH)
        ncols = len(list(_REGION_COLS))
        nrows = _REGION_Y[1] - _REGION_Y[0]
        self.assertEqual(n, ncols * nrows)
        bad_cols = set(np.unique(np.where(mask)[1]))
        self.assertTrue(bad_cols <= set(_REGION_COLS))
        mism = 0
        for y in range(_REGION_Y[0], _REGION_Y[1]):
            for x in range(_REGION_COLS.start, _REGION_COLS.stop):
                if not mask[y, x]:
                    continue
                lv = rv = None
                lx = x - 2
                while lx >= 0:
                    if lx not in _REGION_COLS:
                        lv = cfa[y, lx]
                        break
                    lx -= 2
                rx = x + 2
                while rx < 640:
                    if rx not in _REGION_COLS:
                        rv = cfa[y, rx]
                        break
                    rx += 2
                refs = [v for v in (lv, rv) if v is not None]
                if refs and abs(fixed[y, x] - np.mean(refs)) > 1e-9:
                    mism += 1
        self.assertEqual(mism, 0)

    def test_synthetic_frame_region_gone(self):
        cfa = bp.g_map(_SYNTH_RAW).astype(np.float64)
        fixed, mask, _ = bp.fix_defect_region(
            cfa, _REGION_COLS, _REGION_Y[0], _REGION_Y[1], _REGION_THRESH)
        raw_row = cfa[100, 502:517]
        fixed_row = fixed[100, 502:517]
        self.assertTrue((raw_row > 180).any())
        self.assertTrue((raw_row < 80).any())
        self.assertTrue((fixed_row >= 80).all())
        for i, x in enumerate(range(502, 517)):
            if not mask[100, x]:
                continue
            lx = x - 2
            while lx in _REGION_COLS:
                lx -= 2
            rx = x + 2
            while rx in _REGION_COLS:
                rx += 2
            refs = [v for v, ok in ((cfa[100, lx], lx >= 0),
                                    (cfa[100, rx], rx < 640)) if ok]
            self.assertLess(abs(fixed_row[i] - np.mean(refs)), 1e-9)


class TestFixDeadPixels(unittest.TestCase):

    def test_single_dead_pixel(self):
        cfa = np.full((10, 10), 100.0)
        cfa[5, 5] = 0.0
        fixed, n = bp.fix_dead_pixels(cfa, threshold=60.0)
        self.assertEqual(n, 1)
        self.assertEqual(fixed[5, 5], 100.0)

    def test_skip_mask_protects_region(self):
        cfa = np.full((10, 10), 100.0)
        cfa[5, 5] = 0.0
        skip = np.zeros_like(cfa, dtype=bool)
        skip[5, 5] = True
        fixed, n = bp.fix_dead_pixels(cfa, threshold=60.0, skip=skip)
        self.assertEqual(n, 0)
        self.assertEqual(fixed[5, 5], 0.0)


class TestRenderStitched(unittest.TestCase):

    def test_output_shape_and_dtype(self):
        rng = np.random.default_rng(42)
        cfa = rng.integers(0, 256, size=(480, 640), dtype=np.uint16).astype(np.uint8)
        rgb = bp.render_stitched(cfa.astype(np.float64))
        self.assertEqual(rgb.shape, (480, 640, 3))
        self.assertEqual(rgb.dtype, np.uint8)

    def test_gray_world_default(self):
        cfa = np.full((480, 640), 128.0)
        rgb = bp.render_stitched(cfa, b_extra=1.0)
        means = [rgb[:, :, i].mean() for i in range(3)]
        self.assertLess(max(means) - min(means), 8.0)

    def test_b_extra_lowers_blue(self):
        rng = np.random.default_rng(7)
        cfa = rng.integers(0, 256, size=(480, 640), dtype=np.uint16).astype(np.uint8)
        rgb_default = bp.render_stitched(cfa.astype(np.float64))
        rgb_lowb = bp.render_stitched(cfa.astype(np.float64), b_extra=0.5)
        self.assertLess(rgb_lowb[:, :, 2].mean(), rgb_default[:, :, 2].mean())


class TestPipeline(unittest.TestCase):

    def test_pipeline_reproducible(self):
        rgb1, s1 = bp.pipeline(_SYNTH_RAW)
        rgb2, s2 = bp.pipeline(_SYNTH_RAW)
        self.assertTrue(np.array_equal(rgb1, rgb2))
        self.assertEqual(s1, s2)

    def test_pipeline_region_stats(self):
        rgb, stats = bp.pipeline(_SYNTH_RAW)
        ncols = len(list(_REGION_COLS))
        nrows = _REGION_Y[1] - _REGION_Y[0]
        self.assertEqual(stats["region_replaced"], ncols * nrows)
        self.assertEqual(stats["region_skipped"], ncols * nrows)
        self.assertGreater(stats["dead_replaced"], 0)
        means = [rgb[:, :, i].mean() for i in range(3)]
        self.assertTrue(all(20 < m < 240 for m in means))


if __name__ == "__main__":
    unittest.main(verbosity=2)
