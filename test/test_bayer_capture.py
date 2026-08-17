#!/usr/bin/env python3
"""Bayer 采集纯函数验证 (test/test_bayer_capture.py) — Layers J/K

纯软件、无硬件依赖, 只 import 仓库根目录的 bayer_capture (import 安全)。

  Layer J  CAM2    cam2_frame() 封装与往返; cam2_parse(): 坏魔数/错误尺寸/
                   截断 -> None, 垃圾前缀后同步恢复
  Layer K  统计    cfa_means(): 逐通道均值精确值, pattern 生效

运行: python3 -m unittest discover -s test -p "test_bayer_capture.py" -v
"""

import os
import struct
import sys
import unittest

import numpy as np

# ---- 允许从任意 cwd 运行 (import 仓库根目录的 bayer_capture) ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bayer_capture as bc  # noqa: E402

W, H = 320, 240
_PAYLOAD = bytes(range(256)) * (W * H // 256)   # 76,800 B 确定性载荷


def _frame(payload=_PAYLOAD):
    return bc.cam2_frame(W, H, payload)


# ======================================================================
# Layer J — CAM2 帧协议
# ======================================================================

class TestCam2Frame(unittest.TestCase):
    """cam2_frame(): 封装; 载荷长度必须等于 W*H。"""

    def test_roundtrip_parse(self):
        frame = _frame()
        got = bc.cam2_parse(frame, W, H)
        self.assertEqual(got, (0, W, H))
        self.assertEqual(frame[8:], _PAYLOAD)

    def test_frame_len_and_header_fields(self):
        frame = _frame()
        self.assertEqual(len(frame), bc.CAM2_HEADER + W * H)
        self.assertEqual(frame[:4], b"CAM2")
        w, h = struct.unpack_from(">HH", frame, 4)
        self.assertEqual((w, h), (W, H))

    def test_bad_payload_len_raises(self):
        with self.assertRaises(ValueError):
            bc.cam2_frame(W, H, _PAYLOAD[:-1])


class TestCam2Parse(unittest.TestCase):
    """cam2_parse(): 滑动窗口同步语义。"""

    def test_rejects_wrong_magic(self):
        frame = b"CAM1" + struct.pack(">HH", W, H) + _PAYLOAD
        self.assertIsNone(bc.cam2_parse(frame, W, H))

    def test_sync_after_garbage_prefix(self):
        garbage = b"\xff\x00\xfe garbage!" * 3   # 无 "CAM2" 子串
        self.assertNotIn(b"CAM2", garbage)
        buf = garbage + _frame()
        got = bc.cam2_parse(buf, W, H)
        self.assertEqual(got, (len(garbage), W, H))

    def test_rejects_wrong_dims(self):
        frame = bc.cam2_frame(640, H, bytes(640 * H))
        self.assertIsNone(bc.cam2_parse(frame, W, H))

    def test_truncated_payload(self):
        frame = _frame()
        for cut in (10, 1):
            with self.subTest(cut=cut):
                self.assertIsNone(bc.cam2_parse(frame[:-cut], W, H))
        # 头部不完整 / 只有魔数
        self.assertIsNone(bc.cam2_parse(_frame()[:6], W, H))
        self.assertIsNone(bc.cam2_parse(b"CAM2", W, H))


# ======================================================================
# Layer K — CFA 统计
# ======================================================================

class TestCfaMeans(unittest.TestCase):
    """cfa_means(): 逐通道 CFA 均值精确值 (统计验证的纯函数底座)。"""

    def test_exact_means_2x2(self):
        # RGGB 单块: R=10(1x), G=20(2x), B=30(1x) -> 均值 (10,20,30)
        cfa = np.array([[10, 20], [20, 30]], dtype=np.uint8)
        self.assertEqual(bc.cfa_means(cfa, "RGGB"),
                         {"R": 10.0, "G": 20.0, "B": 30.0})

    def test_exact_means_uneven_counts(self):
        # 4x2: R 出现 2 次, G 4 次, B 2 次 -> 均值仍 (10,20,30)
        cfa = np.array([[10, 20], [20, 30], [10, 20], [20, 30]],
                       dtype=np.uint8)
        self.assertEqual(bc.cfa_means(cfa, "RGGB"),
                         {"R": 10.0, "G": 20.0, "B": 30.0})

    def test_means_reflect_pattern(self):
        # 同一数组在 BGGR 布局下: (0,0) 变 B, (1,1) 变 R -> 均值交换
        cfa = np.array([[10, 20], [20, 30]], dtype=np.uint8)
        self.assertEqual(bc.cfa_means(cfa, "BGGR"),
                         {"R": 30.0, "G": 20.0, "B": 10.0})


if __name__ == "__main__":
    unittest.main()
