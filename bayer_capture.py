#!/usr/bin/env python3
"""Bayer 采集纯函数层 (bayer_capture.py)

无硬件、无串口的纯函数 (import 安全, 无模块级 sys.argv):
  cam2_frame / cam2_parse   : "CAM2" 帧封装与滑动窗口同步解析
  stitch_halves             : 上/下半帧 (640x240) -> 640x480 vstack
  encode_bayer_window       : 'T' 命令参数 (upper -> 0x00, lower -> 0x01)
  cfa_means                 : 逐通道 CFA 均值 (分离度统计)

CLI (pyserial 采集) 在 main() 内, __main__ 守卫 -> 可被 unittest import。
"""

import struct

import numpy as np

from bayer_demosaic import DEFAULT_PATTERN

CAM2_MAGIC = b"CAM2"
CAM2_HEADER = 8  # magic(4) + W(u16 BE) + H(u16 BE)


def cam2_frame(w, h, payload):
    """封装一帧 CAM2: b"CAM2" + W(u16 BE) + H(u16 BE) + payload (len == w*h)。"""
    payload = bytes(payload)
    if len(payload) != w * h:
        raise ValueError(f"payload {len(payload)} B != {w}x{h}={w*h} B")
    return CAM2_MAGIC + struct.pack(">HH", w, h) + payload


def cam2_parse(buf, expected_w, expected_h):
    """在 buf 中查找第一帧完整 CAM2 帧 (滑动窗口同步语义)。

    返回 (frame_start, w, h) | None。完整帧判定: magic + W/H == expected,
    且 frame_start + CAM2_HEADER + w*h <= len(buf)。调用方取走该帧后丢弃
    前缀, 保留尾部残余继续累积。
    """
    idx = 0
    while True:
        idx = buf.find(CAM2_MAGIC, idx)
        if idx < 0:
            return None
        if idx + CAM2_HEADER <= len(buf):
            w, h = struct.unpack_from(">HH", buf, idx + 4)
            if w == expected_w and h == expected_h and \
               idx + CAM2_HEADER + w * h <= len(buf):
                return (idx, w, h)
        idx += 1


def stitch_halves(upper, lower):
    """上/下半帧 -> 全帧: np.vstack (行 0..239=upper, 240..479=lower)。

    传感器窗口: 上窗行 15..254, 下窗行 252..491 -> 3 行重叠 (252..254),
    为窗口寄存器方案的已知伪影; 逐半统计才是验证杠杆, 缝合不去重。
    """
    if upper.shape != lower.shape:
        raise ValueError(f"half shape mismatch: {upper.shape} vs {lower.shape}")
    return np.vstack([upper, lower])


def encode_bayer_window(half):
    """'T' 命令参数字节: upper -> 0x00, lower -> 0x01 (固件契约)。"""
    if half == "upper":
        return b"\x00"
    if half == "lower":
        return b"\x01"
    raise ValueError(f"half must be 'upper' or 'lower', got {half!r}")


def cfa_means(cfa, pattern=DEFAULT_PATTERN):
    """逐通道 CFA 均值: {'R': float, 'G': float, 'B': float} (uint8 采样)。"""
    h, w = cfa.shape
    idx = (np.arange(h)[:, None] & 1) * 2 + (np.arange(w)[None, :] & 1)
    cmap = np.array(list(pattern))[idx]
    out = {}
    for ch in "RGB":
        vals = cfa[cmap == ch]
        out[ch] = float(vals.mean()) if vals.size else 0.0
    return out
