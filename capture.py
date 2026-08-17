#!/usr/bin/env python3
"""OV7670 frame receiver - reads "CAM1"+W(2)+H(2)+RGB565 raw stream from USB CDC,
   saves each frame as a BMP (viewable everywhere, zero deps)."""
import serial, struct, sys, os, time, glob

def find_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None

PORT   = find_port() or '/dev/cu.usbmodem141101'
OUTDIR = 'frames'
NFRAMES = 1

def rgb565_to_bmp(w, h, raw):
    """Convert RGB565 raw bytes -> 24-bit BMP bytes.

    NOTE: the RP2040 stream is BIG-ENDIAN per pixel (the OV7670 outputs the
    high byte first and the PIO preserves that order), so byte i is the HIGH
    byte. Parsing it little-endian swaps R and B and scrambles the G bits.
    """
    px = bytearray(w * h * 3)
    for i in range(w * h):
        v = (raw[i * 2] << 8) | raw[i * 2 + 1]
        r = ((v >> 11) & 0x1F); r = (r << 3) | (r >> 2)
        g = ((v >> 5)  & 0x3F); g = (g << 2) | (g >> 4)
        b = (v & 0x1F);         b = (b << 3) | (b >> 2)
        px[i * 3]     = b  # BMP stores BGR
        px[i * 3 + 1] = g
        px[i * 3 + 2] = r
    row_size = (w * 3 + 3) & ~3
    pad = row_size - w * 3
    pix_size = row_size * h
    header = b'BM'
    header += struct.pack('<IHHI', 54 + pix_size, 0, 0, 54)
    header += struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, pix_size, 2835, 2835, 0, 0)
    body = bytearray()
    # BMPs with a positive height are stored BOTTOM-UP: the first row in the
    # file is the LAST image row. Writing rows top-down here would flip the
    # image, so iterate y in reverse.
    for y in range(h - 1, -1, -1):
        row = px[y * w * 3:(y + 1) * w * 3]
        body += row + b'\x00' * pad
    return header + bytes(body)

def read_exact(s, n, timeout=5):
    buf = b''
    t0 = time.time()
    while len(buf) < n and time.time() - t0 < timeout:
        buf += s.read(n - len(buf))
    return buf

def main():
    global PORT, OUTDIR, NFRAMES
    PORT   = sys.argv[1] if len(sys.argv) > 1 else '/dev/cu.usbmodem141101'
    OUTDIR = sys.argv[2] if len(sys.argv) > 2 else 'frames'
    NFRAMES = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    os.makedirs(OUTDIR, exist_ok=True)

    s = serial.Serial(PORT, 115200, timeout=2)
    print(f"Listening on {PORT}, saving to {OUTDIR}/ ...")
    s.reset_input_buffer()

    saved = 0
    while saved < NFRAMES:
        # sync to magic
        magic = b''
        while len(magic) < 4:
            b = s.read(1)
            if not b: continue
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
        raw = read_exact(s, size, timeout=10)
        if len(raw) < size:
            print(f"frame truncated: got {len(raw)}/{size}"); continue
        fname = os.path.join(OUTDIR, f"frame_{saved:03d}_{w}x{h}.bmp")
        with open(fname, 'wb') as f:
            f.write(rgb565_to_bmp(w, h, raw))
        print(f"  saved {fname} ({len(raw)} bytes raw)")
        saved += 1

    s.close()
    print("done")

if __name__ == '__main__':
    main()
