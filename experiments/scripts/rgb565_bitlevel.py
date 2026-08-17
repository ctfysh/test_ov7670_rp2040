#!/usr/bin/env python3
"""RGB565 bit-order A/B diagnostic: capture CAM1 raw bytes and run the same
bit-level forensics used for the raw-Bayer 6-bit decode question.

Question: is the raw-mode 6-bit pattern (b1=MSB, bit0==bit7 mirror, bit2~0)
a SENSOR raw-mode property or an ELECTRICAL defect (D0<->D7 short / D2
floating)?

Decider: if RGB565 bytes (COM7=0x14 path, all 8 pins = live data) show all 8
bits independent (no bit0==bit7 mirror, no stuck bit2, sensible spatial
correlation on every bit) -> the mirror/stuck pattern is raw-mode-specific
= sensor-side behavior. If RGB565 reproduces the mirror/stuck pattern ->
wiring/sensor hardware fault.

Usage: python3 rgb565_bitlevel.py <port> [nframes]
Writes raw frames to outdir_rgb565/ and prints the per-bit stats table.
"""
import serial, struct, sys, os, time, math

PORT    = sys.argv[1] if len(sys.argv) > 1 else '/dev/cu.usbmodem14101'
NFRAMES = int(sys.argv[2]) if len(sys.argv) > 2 else 2
OUTDIR  = 'outdir_rgb565'
W, H = 320, 240
FRAME_BYTES = W * H * 2


def read_exact(s, n, timeout=10):
    buf = b''
    t0 = time.time()
    while len(buf) < n and time.time() - t0 < timeout:
        buf += s.read(n - len(buf))
    return buf


def bit_stats(data):
    """Per-bit stats across the byte stream:
       - ones fraction (0..1): stuck bits cluster at 0 or 1
       - spatial correlation: corr between consecutive bytes' same bit
         (image data correlates strongly on high bits, ~0 on noise)
       - cross-bit equality: P(bit i == bit j) for the mirror test
    """
    n = len(data)
    ones = [0.0] * 8
    # cross-bit equality matrix
    eq = [[0.0] * 8 for _ in range(8)]
    prev = None
    sp_corr = [0.0] * 8
    # sample up to 400k bytes for speed
    step = max(1, n // 400000)
    idx = list(range(0, n, step))
    n_s = len(idx)
    for k, i in enumerate(idx):
        b = data[i]
        for bit in range(8):
            if (b >> bit) & 1:
                ones[bit] += 1.0
        for i_bit in range(8):
            bi = (b >> i_bit) & 1
            for j_bit in range(8):
                bj = (b >> j_bit) & 1
                if bi == bj:
                    eq[i_bit][j_bit] += 1.0
        if prev is not None:
            for bit in range(8):
                if ((b >> bit) & 1) == ((prev >> bit) & 1):
                    sp_corr[bit] += 1.0
        prev = b
    for bit in range(8):
        ones[bit] /= n_s
        sp_corr[bit] /= max(1, n_s - 1)
    for i in range(8):
        for j in range(8):
            eq[i][j] /= n_s
    return ones, eq, sp_corr


def main():
    s = serial.Serial(PORT, 115200, timeout=2)
    print(f"Listening on {PORT} for CAM1 RGB565 frames ({W}x{H}, {FRAME_BYTES} B)...")
    s.reset_input_buffer()
    os.makedirs(OUTDIR, exist_ok=True)

    saved = 0
    while saved < NFRAMES:
        magic = b''
        while len(magic) < 4:
            b = s.read(1)
            if not b:
                continue
            magic = (magic + b)[-4:]
            if magic == b'CAM1':
                break
        if magic != b'CAM1':
            print("sync timeout"); break
        hdr = read_exact(s, 4)
        if len(hdr) < 4:
            print("header timeout"); break
        w, h = struct.unpack('>HH', hdr)
        size = w * h * 2
        raw = read_exact(s, size, timeout=15)
        if len(raw) < size:
            print(f"frame truncated: got {len(raw)}/{size}; retrying"); continue
        fname = os.path.join(OUTDIR, f"rgb565_{saved:03d}_{w}x{h}.raw")
        with open(fname, 'wb') as f:
            f.write(raw)
        print(f"  saved {fname} ({len(raw)} bytes)")
        saved += 1

        ones, eq, sp = bit_stats(raw)
        print(f"  ---- frame {saved-1} bit stats ----")
        print(f"  {'bit':>4} {'ones':>8} {'P(b==prev)':>10}")
        for bit in range(8):
            print(f"  {bit:>4} {ones[bit]:>8.4f} {sp[bit]:>10.4f}")
        print(f"  P(bit0==bit7) = {eq[0][7]:.6f}")
        print(f"  P(bit1==bit7) = {eq[1][7]:.6f}")
        print(f"  P(bit2==bit7) = {eq[2][7]:.6f}")
    s.close()
    print("done")


if __name__ == '__main__':
    main()
