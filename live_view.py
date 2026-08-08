#!/usr/bin/env python3
"""OV7670 live camera viewer.

Reads the continuous CAM1 stream (firmware streams frames back-to-back:
magic "CAM1" + W:H big-endian + raw RGB565 big-endian pixels) and displays
it in real time with pygame.

Keys:
  S        save current frame as BMP (live_NNN.bmp)
  Q / Esc  quit

Usage:  python3 live_view.py [port] [scale]
  port   serial port (default: first /dev/cu.usbmodem*)
  scale  display scale factor (default: 2)

The viewer adapts to whatever WxH the firmware sends (QVGA 320x240 default;
if you rebuild with FRAME_W=160 FRAME_H=120 it just works).  Decoding is
vectorized with numpy for real-time throughput.
"""
import glob
import struct
import sys
import time

import numpy as np
import pygame

import capture  # reuse rgb565_to_bmp for S-key snapshots


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


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    scale = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    if not port:
        print("No /dev/cu.usbmodem* device found; pass a port explicitly.")
        sys.exit(1)

    import serial
    s = serial.Serial(port, 115200, timeout=2)
    s.reset_input_buffer()
    print(f"Connected {port}. Waiting for frames...")

    pygame.init()
    screen = None
    font = pygame.font.SysFont("menlo", 16)
    clock = pygame.time.Clock()

    W = H = 0
    buf = bytearray()
    saved = 0
    frames = 0
    fps = 0.0
    t0 = time.time()
    running = True

    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif ev.key == pygame.K_s and W and H:
                    with open(f"live_{saved:03d}.bmp", "wb") as f:
                        f.write(capture.rgb565_to_bmp(W, H, bytes(buf)))
                    print(f"  saved live_{saved:03d}.bmp")
                    saved += 1

        # --- sync to "CAM1" magic ---
        magic = b""
        while len(magic) < 4:
            ch = s.read(1)
            if not ch:
                continue
            magic = (magic + ch)[-4:]
            if magic == b"CAM1":
                break
        if magic != b"CAM1":
            continue

        hdr = b""
        while len(hdr) < 4:
            ch = s.read(4 - len(hdr))
            if not ch:
                break
            hdr += ch
        if len(hdr) < 4:
            continue
        w, h = struct.unpack(">HH", hdr)
        size = w * h * 2
        if (w, h) != (W, H):
            W, H = w, h
            buf = bytearray(size)
            if screen is None:
                screen = pygame.display.set_mode((W * scale, H * scale))
                pygame.display.set_caption(
                    f"OV7670 Live {W}x{H}  S=save Q=quit")
            print(f"resolution {W}x{H}, size {size} B/frame")

        got = 0
        while got < size:
            ch = s.read(size - got)
            if not ch:
                break
            buf[got:got + len(ch)] = ch
            got += len(ch)
        if got < size:
            continue  # truncated frame, re-sync

        # --- decode + display ---
        img = decode_rgb565(bytes(buf), W, H)
        surf = pygame.surfarray.make_surface(np.transpose(img, (1, 0, 2)))
        screen.blit(pygame.transform.scale(surf, (W * scale, H * scale)), (0, 0))
        frames += 1
        now = time.time()
        if now - t0 >= 1.0:
            fps = frames / (now - t0)
            frames = 0
            t0 = now
        screen.blit(font.render(f"{fps:.1f} FPS", True, (0, 255, 0)), (8, 8))
        pygame.display.flip()
        clock.tick(120)

    s.close()
    pygame.quit()


if __name__ == "__main__":
    main()
