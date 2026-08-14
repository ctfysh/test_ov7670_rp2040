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


def stitch_halves(upper, lower):
    """上/下半帧 -> 全帧: np.vstack (行 0..239=upper, 240..479=lower)。

    传感器窗口 (VSTOP 排他, 实测): 上窗行 15..254, 下窗行 252..491 ->
    3 行真实重叠 (252..254), 重叠行在两半帧中内容一致 (曝光已锁定),
    vstack 不去重, 重叠行重复一次为已知伪影, 无亮度缝。
    """
    if upper.shape != lower.shape:
        raise ValueError(f"half shape mismatch: {upper.shape} vs {lower.shape}")
    return np.vstack([upper, lower])


def overlap_corr(upper, lower, n=3):
    """重叠行相关: upper 末 n 行 vs lower 前 n 行 (均为 sensor 行 252..254)。

    两半帧在真实重叠区 (3 行) 内容必须一致 (曝光锁定)。corr 显著 < 0.8
    表示抓到的是陈旧/错窗口帧 (采集竞态), 而非窗口本身出错。
    """
    a = upper[-n:].ravel().astype(np.float32)
    b = lower[:n].ravel().astype(np.float32)
    a = a - a.mean()
    b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / (d + 1e-9))


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


def _wait_ack(stream, param, timeout_s=5.0):
    """Wait for 'DBG1' 0xF9 <param>; True on match, False on 0xFF/timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        i = stream.buf.find(b"DBG1\xF9")
        if i >= 0 and i + 6 <= len(stream.buf):
            got = stream.buf[i + 5]
            stream.drop(i + 6)
            return got == param
        if not stream.fill():
            time.sleep(0.05)
    return False


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


def _switch_window(stream, ser, half, settle_s=0.7):
    """发 'T' 切窗并等待新窗帧稳定, 返回 True=ack OK。

    竞态说明: 固件在收到 'T' 后的下一个 VSYNC 才应用新窗口, 而旧窗口帧
    此刻仍在 USB 线上传输 (153600 B @ ~660 KB/s ≈ 233 ms/帧)。若在 ack
    后立即 _capture_one, 会抓到缓冲/线路上遗留的**旧窗口完整帧** (例如
    'lower' 抓到切换前的 'upper' 内容)。因此必须: ack -> settle 足够久
    (旧帧全部下线路) -> reset_input_buffer + 清空 stream.buf, 再采集。
    如此 _capture_one 抓到的第一帧必为新窗口帧。
    """
    param = encode_bayer_window(half)
    ser.write(b"T" + param)
    if not _wait_ack(stream, param[0]):
        return False
    time.sleep(settle_s)
    ser.reset_input_buffer()
    stream.buf = b""
    return True


def main(argv=None):
    import argparse
    import json
    import os

    import serial  # CLI-only dependency

    p = argparse.ArgumentParser(
        description="OV7670 raw Bayer half-frame capture (CAM2 protocol)")
    p.add_argument("--port", default=None, help="CDC port (auto-detect if omitted)")
    p.add_argument("--out", default="bayer_frames", help="output directory")
    p.add_argument("--pairs", type=int, default=3, help="upper/lower frame pairs (default 3)")
    p.add_argument("--pattern", default=DEFAULT_PATTERN, help="CFA pattern (default RGGB)")
    p.add_argument("--bmp", action="store_true", help="also write demosaiced 640x480 BMP per pair")
    args = p.parse_args(argv)

    W, H = 640, 240  # pinned half-frame geometry (firmware RAW_BAYER)
    port = args.port or _find_port()
    os.makedirs(args.out, exist_ok=True)

    ser = serial.Serial(port, 115200, timeout=2)
    print(f"RAW_BAYER capture on {port} -> {args.out}/")
    ser.reset_input_buffer()
    stream = _Stream(ser)

    # First command MUST be 'T' upper: after init the window is full VGA.
    if not _switch_window(stream, ser, "upper"):
        raise SystemExit("no 'T' upper ack from firmware")

    frames = []
    for i in range(args.pairs):
        if not _switch_window(stream, ser, "upper"):
            raise SystemExit(f"pair {i}: no 'T' upper ack")
        upper = _capture_one(stream, W, H)
        if upper is None:
            raise SystemExit(f"pair {i}: upper frame sync timeout")
        if not _switch_window(stream, ser, "lower"):
            raise SystemExit(f"pair {i}: no 'T' lower ack")
        lower = _capture_one(stream, W, H)
        if lower is None:
            raise SystemExit(f"pair {i}: lower frame sync timeout")

        up = np.frombuffer(upper, dtype=np.uint8).reshape(H, W)
        lo = np.frombuffer(lower, dtype=np.uint8).reshape(H, W)
        full = stitch_halves(up, lo)

        # Overlap self-check: upper 末 3 行 == lower 前 3 行 (sensor 252..254)
        ov = overlap_corr(up, lo)
        flag = "OK" if ov > 0.6 else "STALE/WRONG WINDOW?"
        print(f"  pair {i}: overlap corr(upper[-3:],lower[:3])={ov:+.3f} [{flag}]")

        with open(os.path.join(args.out, f"upper_{i:03d}.raw"), 'wb') as f:
            f.write(upper)
        with open(os.path.join(args.out, f"lower_{i:03d}.raw"), 'wb') as f:
            f.write(lower)
        with open(os.path.join(args.out, f"frame_{i:03d}_640x480.raw"), 'wb') as f:
            f.write(full.tobytes())

        rec = {
            "index": i,
            "upper_mean": cfa_means(up, args.pattern),
            "lower_mean": cfa_means(lo, args.pattern),
            "stitched_mean": cfa_means(full, args.pattern),
        }
        frames.append(rec)

        if args.bmp:
            import bayer_demosaic
            rgb = bayer_demosaic.demosaic_bayer(full, pattern=args.pattern)
            fh, fw = full.shape  # 拼接后全帧 640x480（不是半帧 640x240）
            with open(os.path.join(args.out, f"frame_{i:03d}_640x480.bmp"), 'wb') as f:
                f.write(bayer_demosaic.rgb_to_bmp(fw, fh, rgb))

        print(f"  pair {i}: upper={rec['upper_mean']} lower={rec['lower_mean']}")

    overall = {}
    for key in ("upper_mean", "lower_mean", "stitched_mean"):
        overall[key] = {ch: sum(f[key][ch] for f in frames) / len(frames)
                        for ch in "RGB"}
    stats = {"pattern": args.pattern, "pairs": args.pairs,
             "frames": frames, "overall": overall}
    with open(os.path.join(args.out, "stats.json"), "w") as f:
        json.dump(stats, f, indent=2)

    ser.close()
    print(f"wrote {args.pairs} pair(s) + stats.json to {args.out}/")


if __name__ == "__main__":
    main()
