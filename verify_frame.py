#!/usr/bin/env python3
"""OV7670 frame verification - syncs to CAM1, prints payload statistics so we can
   tell a real image (many distinct byte values, high entropy) from the all-zeros
   payload the IN_SHIFTDIR bug produced. Saves a BMP too, reusing capture.py logic."""
import serial, struct, sys, os, time

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/cu.usbmodem141101'
OUTDIR = 'frames'
NFRAMES = int(sys.argv[2]) if len(sys.argv) > 2 else 1

os.makedirs(OUTDIR, exist_ok=True)

def rgb565_to_bmp(w, h, raw):
    px = bytearray(w * h * 3)
    for i in range(w * h):
        v = raw[i * 2] | (raw[i * 2 + 1] << 8)
        r = ((v >> 11) & 0x1F); r = (r << 3) | (r >> 2)
        g = ((v >> 5)  & 0x3F); g = (g << 2) | (g >> 4)
        b = (v & 0x1F);         b = (b << 3) | (b >> 2)
        px[i * 3]     = b
        px[i * 3 + 1] = g
        px[i * 3 + 2] = r
    row_size = (w * 3 + 3) & ~3
    pad = row_size - w * 3
    pix_size = row_size * h
    header = b'BM'
    header += struct.pack('<IHHI', 54 + pix_size, 0, 0, 54)
    header += struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, pix_size, 2835, 2835, 0, 0)
    body = bytearray()
    for y in range(h):
        row = px[y * w * 3:(y + 1) * w * 3]
        body += row + b'\x00' * pad
    return header + bytes(body)

s = serial.Serial(PORT, 115200, timeout=2)
print(f"Listening on {PORT} ...")
s.reset_input_buffer()

def read_exact(n, timeout=5):
    buf = b''
    t0 = time.time()
    while len(buf) < n and time.time() - t0 < timeout:
        buf += s.read(n - len(buf))
    return buf

saved = 0
while saved < NFRAMES:
    magic = b''
    while len(magic) < 4:
        b = s.read(1)
        if not b: continue
        magic = (magic + b)[-4:]
        if magic == b'CAM1':
            break
    if magic != b'CAM1':
        print("sync timeout"); break
    hdr = read_exact(4)
    if len(hdr) < 4:
        print("header timeout"); break
    w, h = struct.unpack('>HH', hdr)
    size = w * h * 2
    raw = read_exact(size, timeout=10)
    if len(raw) < size:
        print(f"frame truncated: got {len(raw)}/{size}"); continue

    nz = sum(1 for x in raw if x != 0)
    distinct = len(set(raw))
    lo = min(raw); hi = max(raw)
    # per-byte-position stats: 0=low byte of RGB565, 1=high byte
    nz_lo = sum(1 for i in range(0, len(raw), 2) if raw[i] != 0)
    nz_hi = sum(1 for i in range(1, len(raw), 2) if raw[i] != 0)
    print(f"  frame {saved}: {w}x{h}, {len(raw)} B, non-zero {nz}/{len(raw)} "
          f"({100.0*nz/len(raw):.1f}%), distinct values {distinct}, "
          f"byte range {lo:#04x}..{hi:#04x}, "
          f"low-byte non-zero {nz_lo}/{len(raw)//2}, high-byte non-zero {nz_hi}/{len(raw)//2}")
    # first 16 bytes raw for eyeballing
    print(f"    first 16 bytes: {raw[:16].hex(' ')}")
    fname = os.path.join(OUTDIR, f"frame_{saved:03d}_{w}x{h}.bmp")
    with open(fname, 'wb') as f:
        f.write(rgb565_to_bmp(w, h, raw))
    print(f"  saved {fname}")
    saved += 1

s.close()
print("done")
