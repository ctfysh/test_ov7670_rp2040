#!/usr/bin/env python3
"""Bayer 数学验证 (test/bayer_math.py) — Layers F/G/H

纯软件、无硬件依赖, 只 import 仓库根目录的 bayer_demosaic (纯函数、import 安全)。

  Layer F  CFA 色彩恒等   cfa_color(): RGGB 默认映射 (spec §6.1), 2x2 平铺, 备选模式
  Layer G  去马赛克数学   demosaic_bayer(): 常量 CFA -> 全图均匀 (含边缘); 线性 G 斜坡
                         -> 内部逐像素精确 (双线性对线性函数精确, G 校正恒等);
                         nearest_neighbor_demosaic(): 2x2 块复制 + 行主序 tie-break
  Layer H  BMP 编码       rgb_to_bmp(): 文件头字段, BGR 序 + bottom-up, 行填充

运行: python3 -m unittest discover -s test -p "test_bayer_math.py" -v
"""

import os
import sys
import unittest

import numpy as np

# ---- 允许从任意 cwd 运行 (import 仓库根目录的 bayer_demosaic) ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bayer_demosaic as bd  # noqa: E402  真实去马赛克实现 (纯函数, import 安全)


# ======================================================================
# Layer F — CFA 色彩恒等
# ======================================================================

class TestCfaColor(unittest.TestCase):
    """cfa_color(): 由 2x2 模式字符串 (行主序) 求 (y,x) 处色彩。"""

    def test_default_pattern_is_rggb(self):
        # spec §6.1 映射: 偶行偶列=R, 偶行奇列=G, 奇行偶列=G, 奇行奇列=B
        self.assertEqual(bd.DEFAULT_PATTERN, "RGGB")

    def test_cfa_color_default_2x2_and_tiling(self):
        cases = {(0, 0): "R", (0, 1): "G", (1, 0): "G", (1, 1): "B",
                 (0, 2): "R", (2, 0): "R", (2, 2): "R",   # 每 2 行/列重复
                 (0, 3): "G", (1, 3): "B", (3, 1): "B",
                 (3, 3): "B"}
        for (y, x), expected in cases.items():
            with self.subTest(y=y, x=x):
                self.assertEqual(bd.cfa_color(bd.DEFAULT_PATTERN, y, x), expected)

    def test_cfa_color_alternative_patterns(self):
        # 模式串按行主序覆盖 2x2 块
        self.assertEqual(bd.cfa_color("BGGR", 0, 0), "B")
        self.assertEqual(bd.cfa_color("BGGR", 0, 1), "G")
        self.assertEqual(bd.cfa_color("BGGR", 1, 0), "G")
        self.assertEqual(bd.cfa_color("BGGR", 1, 1), "R")
        self.assertEqual(bd.cfa_color("GRBG", 0, 0), "G")
        self.assertEqual(bd.cfa_color("GRBG", 0, 1), "R")
        self.assertEqual(bd.cfa_color("GRBG", 1, 1), "G")
        self.assertEqual(bd.cfa_color("GBRG", 0, 1), "B")
        self.assertEqual(bd.cfa_color("GBRG", 1, 0), "R")


# ======================================================================
# Layer G — 去马赛克数学
# ======================================================================

def _make_cfa(h, w, pattern, r, g, b):
    """按 pattern 填充 CFA: R 位置=r, G 位置=g, B 位置=b (uint8)。"""
    cfa = np.zeros((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            ch = bd.cfa_color(pattern, y, x)
            cfa[y, x] = {"R": r, "G": g, "B": b}[ch]
    return cfa


class TestDemosaicBilinear(unittest.TestCase):
    """demosaic_bayer(): 双线性 G-first + G 校正 R/B, 有效掩码边缘处理。"""

    def test_constant_cfa_uniform_output(self):
        # 常量 CFA -> 全图均匀 (200,100,50), 边缘/角点也精确:
        # 常量插值仍是常量, 校正比恒为 1:1。
        h, w = 6, 8
        cfa = _make_cfa(h, w, bd.DEFAULT_PATTERN, 200, 100, 50)
        rgb = bd.demosaic_bayer(cfa)
        self.assertEqual(rgb.shape, (h, w, 3))
        expected = np.full((h, w, 3), (200, 100, 50), dtype=np.uint8)
        np.testing.assert_array_equal(rgb, expected)

    def test_linear_g_ramp_interior_exact(self):
        # G(y,x)=4y+2x+100 为线性: 双线性插值精确复现; R=250/B=30 常量,
        # G 在 R/B 邻域位置亦线性 -> G 校正恒等。内部 (2..h-3, 2..w-3)
        # 逐像素精确。注意最内圈 (1..h-2, 1..w-2) 的 R/B 采样会触及图像
        # 边缘(有效掩码邻域被截断), 校正比不再恒等, 故不在此断言精确性。
        h, w = 8, 10
        cfa = np.zeros((h, w), dtype=np.uint8)
        for y in range(h):
            for x in range(w):
                ch = bd.cfa_color(bd.DEFAULT_PATTERN, y, x)
                cfa[y, x] = 250 if ch == "R" else (30 if ch == "B" else 4 * y + 2 * x + 100)
        rgb = bd.demosaic_bayer(cfa)
        for y in range(2, h - 2):
            for x in range(2, w - 2):
                with self.subTest(y=y, x=x):
                    self.assertEqual(int(rgb[y, x, 1]), 4 * y + 2 * x + 100)
                    self.assertEqual(int(rgb[y, x, 0]), 250)
                    self.assertEqual(int(rgb[y, x, 2]), 30)


class TestNearestNeighbor(unittest.TestCase):
    """nearest_neighbor_demosaic(): 2x2 块复制, 行主序 tie-break。"""

    def test_block_replication_4x4(self):
        # 4 个独立块 A/B/C/D, 每块 (R,G,B) 不同:
        #   A=(10,11,12) B=(20,21,22) C=(30,31,32) D=(40,41,42)
        # 每 2x2 输出块均匀 = 该块值: R=cfa[by,bx], G=cfa[by,bx+1], B=cfa[by+1,bx+1]
        cfa = np.array([
            [10, 11, 20, 21],
            [11, 12, 21, 22],
            [30, 31, 40, 41],
            [31, 32, 41, 42],
        ], dtype=np.uint8)
        rgb = bd.nearest_neighbor_demosaic(cfa)
        self.assertEqual(rgb.shape, (4, 4, 3))
        blocks = {(0, 0): (10, 11, 12), (0, 2): (20, 21, 22),
                  (2, 0): (30, 31, 32), (2, 2): (40, 41, 42)}
        for (by, bx), expected in blocks.items():
            for dy in range(2):
                for dx in range(2):
                    with self.subTest(by=by, bx=bx, dy=dy, dx=dx):
                        np.testing.assert_array_equal(
                            rgb[by + dy, bx + dx], expected)

    def test_block_replication_non_square(self):
        # 2x4 (h=2): 两个并排块, 验证非方形尺寸处理
        cfa = np.array([
            [10, 11, 20, 21],
            [11, 12, 21, 22],
        ], dtype=np.uint8)
        rgb = bd.nearest_neighbor_demosaic(cfa)
        self.assertEqual(rgb.shape, (2, 4, 3))
        np.testing.assert_array_equal(rgb[:, 0:2],
                                      np.full((2, 2, 3), (10, 11, 12), dtype=np.uint8))
        np.testing.assert_array_equal(rgb[:, 2:4],
                                      np.full((2, 2, 3), (20, 21, 22), dtype=np.uint8))


class TestDemosaicContract(unittest.TestCase):
    """两个去马赛克入口的返回契约。"""

    def test_output_shape_and_dtype(self):
        cfa = _make_cfa(4, 6, bd.DEFAULT_PATTERN, 128, 64, 32)
        for fn in (bd.demosaic_bayer, bd.nearest_neighbor_demosaic):
            with self.subTest(fn=fn.__name__):
                out = fn(cfa)
                self.assertEqual(out.shape, (4, 6, 3))
                self.assertEqual(out.dtype, np.uint8)


# ======================================================================
# Layer H — BMP 编码
# ======================================================================

class TestBmpWriter(unittest.TestCase):
    """rgb_to_bmp(): 54B 头 + 24bpp BGR + bottom-up + 行填充 (仿 capture.py)。"""

    def test_header_fields(self):
        w, h = 4, 3
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        data = bd.rgb_to_bmp(w, h, rgb)
        self.assertEqual(data[:2], b"BM")
        row_size = (w * 3 + 3) & ~3          # 12
        self.assertEqual(len(data), 54 + row_size * h)      # 90
        self.assertEqual(int.from_bytes(data[2:6], "little"), len(data))
        self.assertEqual(int.from_bytes(data[10:14], "little"), 54)
        self.assertEqual(int.from_bytes(data[18:22], "little"), w)
        self.assertEqual(int.from_bytes(data[22:26], "little"), h)
        self.assertEqual(int.from_bytes(data[28:30], "little"), 24)
        self.assertEqual(int.from_bytes(data[30:34], "little"), 0)   # BI_RGB

    def test_pixel_order_bgr_bottom_up(self):
        # 3x2: 顶行 (0,0)=红, 底行 (1,2)=蓝 -> 存储 BGR 且行序 bottom-up
        w, h = 3, 2
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[0, 0] = (255, 0, 0)
        rgb[1, 2] = (0, 0, 255)
        data = bd.rgb_to_bmp(w, h, rgb)
        row_size = (w * 3 + 3) & ~3          # 12
        px = data[54:]

        def pixel(x, y):
            off = row_size * (h - 1 - y) + x * 3
            return (px[off], px[off + 1], px[off + 2])

        # 顶行(红) 存在底行槽位 -> 数据顺序即 bottom-up 证据
        self.assertEqual(pixel(0, 0), (0, 0, 255))    # 红 -> BGR(0,0,255)
        self.assertEqual(pixel(2, 1), (255, 0, 0))    # 蓝 -> BGR(255,0,0)

    def test_row_padding(self):
        w, h = 5, 1                                   # row_size=(15+3)&~3=16
        rgb = np.full((h, w, 3), 7, dtype=np.uint8)
        data = bd.rgb_to_bmp(w, h, rgb)
        self.assertEqual(len(data), 54 + 16)
        self.assertEqual(data[54:54 + 15], b"\x07" * 15)
        self.assertEqual(data[54 + 15], 0)            # 填充字节


if __name__ == "__main__":
    unittest.main()
