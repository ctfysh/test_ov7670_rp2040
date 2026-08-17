#!/usr/bin/env python3
"""Bayer 采集纯函数层 (bayer_capture.py)

无硬件、无串口的纯函数 (import 安全, 无模块级 sys.argv):
  cam2_frame / cam2_parse   : "CAM2" 帧封装与滑动窗口同步解析
  cfa_means                 : 逐通道 CFA 均值 (分离度统计)

CLI (pyserial 采集) 在 main() 内, __main__ 守卫 -> 可被 unittest import。
"""

import struct
import time

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


# ---- CLI (pyserial; imported inside main() so the pure layer stays dep-free) ----

def _find_port():
    """Auto-detect the YD-RP2040 CDC port (macOS: /dev/cu.usbmodem*)."""
    try:
        import serial.tools.list_ports
        cdc = [p.device for p in serial.tools.list_ports.comports()
               if 'usbmodem' in p.device or 'usbserial' in p.device or 'ttyACM' in p.device]
    except Exception:
        cdc = []
    if len(cdc) == 1:
        return cdc[0]
    if len(cdc) > 1:
        return cdc[0]  # first match; --port overrides
    raise SystemExit("no CDC port found; pass --port")


class _Stream:
    """Buffered serial reader: big read(4096) + sliding-window sync (never
    byte-at-a-time). DBG1 diagnostics pass through untouched."""

    def __init__(self, ser):
        self.ser = ser
        self.buf = b""

    def fill(self):
        chunk = self.ser.read(4096)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def drop(self, n):
        self.buf = self.buf[n:]


def _capture_one(stream, w, h, timeout_s=10.0):
    """Capture one complete CAM2 frame; returns payload bytes or None."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        hit = cam2_parse(stream.buf, w, h)
        if hit is not None:
            start, fw, fh = hit
            end = start + CAM2_HEADER + fw * fh
            frame = stream.buf[start + CAM2_HEADER:end]
            stream.drop(end)
            return frame
        if not stream.fill():
            time.sleep(0.05)
    return None


def main(argv=None):
    import argparse
    import json
    import os

    import serial  # CLI-only dependency

    p = argparse.ArgumentParser(
        description="OV7670 raw Bayer capture (CAM2 protocol, single 320x240 window)")
    p.add_argument("--port", default=None, help="CDC port (auto-detect if omitted)")
    p.add_argument("--out", default="bayer_frames", help="output directory")
    p.add_argument("--frames", type=int, default=3, help="frames to capture (default 3)")
    p.add_argument("--pattern", default=DEFAULT_PATTERN, help="CFA pattern (default RGGB)")
    p.add_argument("--bmp", action="store_true", help="also write demosaiced BMP per frame")
    args = p.parse_args(argv)

    W, H = 320, 240
    port = args.port or _find_port()
    os.makedirs(args.out, exist_ok=True)

    ser = serial.Serial(port, 115200, timeout=2)
    print(f"RAW_BAYER capture on {port} -> {args.out}/")
    ser.reset_input_buffer()
    stream = _Stream(ser)

    frames = []
    for i in range(args.frames):
        raw = _capture_one(stream, W, H)
        if raw is None:
            raise SystemExit(f"frame {i}: sync timeout")

        cfa = np.frombuffer(raw, dtype=np.uint8).reshape(H, W)

        with open(os.path.join(args.out, f"frame_{i:03d}_{W}x{H}.raw"), 'wb') as f:
            f.write(raw)

        rec = {
            "index": i,
            "cfa_mean": cfa_means(cfa, args.pattern),
        }
        frames.append(rec)

        if args.bmp:
            import bayer_demosaic
            rgb = bayer_demosaic.demosaic_bayer(cfa, pattern=args.pattern)
            with open(os.path.join(args.out, f"frame_{i:03d}_{W}x{H}.bmp"), 'wb') as f:
                f.write(bayer_demosaic.rgb_to_bmp(W, H, rgb))

        print(f"  frame {i}: cfa_mean={rec['cfa_mean']}")

    overall = {ch: sum(f["cfa_mean"][ch] for f in frames) / len(frames)
               for ch in "RGB"}
    stats = {"pattern": args.pattern, "frames": args.frames,
             "width": W, "height": H,
             "per_frame": frames, "overall": overall}
    with open(os.path.join(args.out, "stats.json"), "w") as f:
        json.dump(stats, f, indent=2)

    ser.close()
    print(f"wrote {args.frames} frame(s) + stats.json to {args.out}/")


if __name__ == "__main__":
    main()
