#!/usr/bin/env python3
"""OV7670 live camera viewer — CAM1 (RGB565) + CAM2 (raw Bayer).

Reads the continuous frame stream (firmware streams frames back-to-back:
magic "CAM1"/"CAM2" + W:H big-endian + raw pixels) and displays it in real
time with pygame.  Protocol is auto-detected from the frame magic:

  CAM1  "CAM1" + W:H + WxHx2  RGB565 big-endian (default QVGA build)
  CAM2  "CAM2" + W:H + WxH    raw 8-bit Bayer CFA (-DRAW_BAYER build)

CAM2 is shown as grayscale by default (raw CFA values); pass --demosaic for
a bilinear-interpolated color preview.

Keys:
  T        toggle upper/lower half window (CAM2 only; sends 'T'+0x00/0x01,
           waits for DBG1+0xF9+param ack, then re-syncs the stream)
  S        save current frame as BMP (live_NNN.bmp)
  Q / Esc  quit

Usage:  python3 live_view.py [port] [scale] [rotate] [--demosaic] [--frames N]
  port     serial port (default: first /dev/cu.usbmodem*)
  scale    display scale factor (default: 2)
  rotate   counter-clockwise rotation in degrees: 0/90/180/270 (default: 90)
  --demosaic  interpolate CAM2 Bayer to color (bilinear)
  --frames N  exit after N displayed frames (default 0 = run forever)

The viewer adapts to whatever WxH the firmware sends (QVGA 320x240 CAM1,
640x240 CAM2 half-frame; FRAME_W/FRAME_H rebuilds just work).  Decoding is
vectorized with numpy.  Rotation is applied on the numpy image before
display, so S-key snapshots are what you see (WYSIWYG).
"""
import glob
import struct
import sys
import time

import numpy as np
import pygame

import capture  # reuse rgb565_to_bmp fallback (unrotated) for S-key snapshots

CAM1_MAGIC = b"CAM1"
CAM2_MAGIC = b"CAM2"
DBG1_F9 = b"DBG1\xF9"  # 'T' ack: DBG1 + 0xF9 + param (0x00 upper / 0x01 lower)
READ_CHUNK = 4096


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def decode_rgb565(raw, w, h):
    """RGB565 big-endian -> (h, w, 3) uint8 RGB array, vectorized."""
    b = np.frombuffer(raw, dtype=np.uint8)
    v = (b[0::2].astype(np.uint16) << 8) | b[1::2].astype(np.uint16)
    r = ((v >> 11) & 0x1F).astype(np.uint8); r = (r << 3) | (r >> 2)
    g = ((v >> 5) & 0x3F).astype(np.uint8);  g = (g << 2) | (g >> 4)
    bl = (v & 0x1F).astype(np.uint8);        bl = (bl << 3) | (bl >> 2)
    return np.dstack([r, g, bl]).reshape(h, w, 3)


def decode_bayer(raw, w, h, demosaic=False, pattern="RGGB"):
    """Raw 8-bit Bayer -> (h, w, 3) uint8 RGB array.

    demosaic=False: grayscale preview (each CFA cell shown as its raw value).
    demosaic=True:  bilinear demosaic via bayer_demosaic (color preview).
    """
    cfa = np.frombuffer(raw, dtype=np.uint8).reshape(h, w)
    if not demosaic:
        return np.dstack([cfa, cfa, cfa])
    import bayer_demosaic
    return bayer_demosaic.demosaic_bayer(cfa, pattern=pattern)


class _Stream:
    """Buffered serial reader: big read(4096) + sliding-window sync (never
    byte-at-a-time).  DBG1 diagnostics pass through untouched."""

    def __init__(self, ser):
        self.ser = ser
        self.buf = b""

    def fill(self):
        chunk = self.ser.read(READ_CHUNK)
        if not chunk:
            return False
        self.buf += chunk
        return True

    def drop(self, n):
        self.buf = self.buf[n:]

    def sync_magic(self):
        """Find the next CAM1/CAM2 magic; drop through it. Returns magic or None."""
        while True:
            i1 = self.buf.find(CAM1_MAGIC)
            i2 = self.buf.find(CAM2_MAGIC)
            hits = [i for i in (i1, i2) if i >= 0]
            if hits:
                i = min(hits)
                self.drop(i + 4)  # consume magic; header follows
                return CAM1_MAGIC if i1 >= 0 and (i2 < 0 or i1 <= i2) else CAM2_MAGIC
            if not self.fill():
                return None

    def read_exact(self, n, timeout_s=5.0):
        t0 = time.time()
        while len(self.buf) < n:
            if time.time() - t0 > timeout_s:
                return None
            if not self.fill():
                time.sleep(0.01)
        out = self.buf[:n]
        self.drop(n)
        return out

    def wait_ack(self, param, timeout_s=5.0):
        """Wait for DBG1+0xF9+param in the stream. True on match, False on timeout."""
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            i = self.buf.find(DBG1_F9)
            if i >= 0 and i + 6 <= len(self.buf):
                got = self.buf[i + 5]
                self.drop(i + 6)
                return got == param
            if not self.fill():
                time.sleep(0.05)
        return False


def main():
    argv = sys.argv[1:]
    demosaic = "--demosaic" in argv
    frames_limit = 0
    clean = [a for a in argv if a != "--demosaic"]
    if "--frames" in clean:
        i = clean.index("--frames")
        frames_limit = int(clean[i + 1])
        del clean[i:i + 2]

    port = clean[0] if len(clean) > 0 else find_port()
    scale = int(clean[1]) if len(clean) > 1 else 2
    rot = int(clean[2]) if len(clean) > 2 else 90
    rot_k = (rot // 90) % 4  # np.rot90 k: 1 = CCW 90deg (rotate left)
    if not port:
        print("No /dev/cu.usbmodem* device found; pass a port explicitly.")
        sys.exit(1)

    import serial
    s = serial.Serial(port, 115200, timeout=2)
    s.reset_input_buffer()
    stream = _Stream(s)
    print(f"Connected {port}. Waiting for frames...")

    pygame.init()
    screen = None
    font = pygame.font.SysFont("menlo", 16)
    clock = pygame.time.Clock()

    W = H = 0
    disp_w = disp_h = 0
    cur_surf = None
    saved = 0
    frames = 0
    total_frames = 0
    fps = 0.0
    t0 = time.time()
    running = True
    mode = "CAM1"
    half = "upper"

    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif ev.key == pygame.K_s and cur_surf is not None:
                    fname = f"live_{saved:03d}.bmp"
                    pygame.image.save(cur_surf, fname)
                    print(f"  saved {fname}")
                    saved += 1
                elif ev.key == pygame.K_t:
                    half = "lower" if half == "upper" else "upper"
                    param = 0x00 if half == "upper" else 0x01
                    s.write(b"T" + bytes([param]))
                    if stream.wait_ack(param):
                        print(f"  half -> {half}")
                    else:
                        print("  'T' ack timeout (CAM1 build?); ignored")
                    s.reset_input_buffer()  # drop stale CAM2 frames mid-switch
                    stream.buf = b""

        magic = stream.sync_magic()
        if magic is None:
            continue
        hdr = stream.read_exact(4)
        if hdr is None:
            continue
        w, h = struct.unpack(">HH", hdr)
        if magic == CAM2_MAGIC:
            mode = "CAM2"
            size = w * h          # 1 byte/px raw Bayer
        else:
            mode = "CAM1"
            size = w * h * 2      # 2 bytes/px RGB565
        if (w, h, magic) != (W, H, magic):
            W, H = w, h
            disp_w, disp_h = (H, W) if rot_k % 2 else (W, H)
            if screen is None:
                screen = pygame.display.set_mode((disp_w * scale, disp_h * scale))
            print(f"{mode} resolution {W}x{H} -> display {disp_w}x{disp_h}")

        payload = stream.read_exact(size)
        if payload is None:
            continue  # truncated frame, re-sync

        # --- decode + rotate + display ---
        if magic == CAM2_MAGIC:
            img = decode_bayer(payload, W, H, demosaic=demosaic)
        else:
            img = decode_rgb565(payload, W, H)
        if rot_k:
            img = np.rot90(img, k=rot_k)  # k=1: rotate left 90deg (CCW)
        surf = pygame.surfarray.make_surface(np.transpose(img, (1, 0, 2)))
        cur_surf = surf
        screen.blit(pygame.transform.scale(surf, (disp_w * scale, disp_h * scale)),
                    (0, 0))
        frames += 1
        total_frames += 1
        now = time.time()
        if now - t0 >= 1.0:
            fps = frames / (now - t0)
            frames = 0
            t0 = now
        tag = f"{mode}" + (f" [{half}]" if mode == "CAM2" else "")
        pygame.display.set_caption(
            f"OV7670 Live {tag} {W}x{H} (rot {rot}deg)  S=save T=half Q=quit")
        screen.blit(font.render(f"{fps:.1f} FPS", True, (0, 255, 0)), (8, 8))
        pygame.display.flip()
        clock.tick(120)

        if frames_limit and total_frames >= frames_limit:
            print(f"displayed {total_frames} frames (limit {frames_limit}); exiting")
            running = False

    s.close()
    pygame.quit()


if __name__ == "__main__":
    main()

