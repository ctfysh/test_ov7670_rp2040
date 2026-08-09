#!/usr/bin/env python3
"""OV7670 live camera viewer - UVC version (ffmpeg backend).

For firmware built with USE_TINYUSB (feat/ov7670-uvc): the video stream
leaves the Pico as a standard USB Video Class (UVC) camera, so there is no
CAM1 serial protocol to parse.

Why ffmpeg instead of OpenCV:
  - On macOS, OpenCV's AVFoundation backend cannot open the UVC device by
    index (it only exposes the built-in FaceTime camera); ffmpeg opens the
    device by NAME reliably.
  - ffmpeg streams raw BGR frames over a pipe; this script just reshapes
    them into numpy arrays for display with cv2.imshow.

Keys:
  S        save current frame as BMP (live_uvc_NNN.bmp)
  Q / Esc  quit

Usage:  python3 live_view_uvc.py [scale] [--list] [--rotate DEG] [--width W] [--height H] [--fps N]
  --list     print the enumerated capture devices and exit
  --rotate   rotation in degrees, counter-clockwise; 90 = rotate left (default: 90)
  scale      display scale factor (default: 2)

Frames are rotated left (CCW) 90 degrees by default to match the mounted
sensor orientation; use --rotate 0 to disable.
"""
import re
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np

FFMPEG_CANDIDATES = [
    "/Users/tiger/mamba/envs/deeplivecam/bin/ffmpeg",  # author's micromamba env
]


def find_ffmpeg():
    """Return a usable ffmpeg path, or None."""
    for p in FFMPEG_CANDIDATES:
        if shutil.which(p):
            return p
    return shutil.which("ffmpeg")


def list_devices(ffmpeg):
    """Run `ffmpeg -f avfoundation -list_devices true` and return [(index, name)]."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True, timeout=15,
    )
    devices = []
    for line in (proc.stderr or "").splitlines():
        m = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if m:
            devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def find_camera_device(ffmpeg):
    """Pick the UVC camera from the device list.

    Prefer a name containing 'RP2040'; then any video device that is not the
    built-in FaceTime camera and not a screen capture.
    """
    devices = list_devices(ffmpeg)
    if not devices:
        return None
    # 1) exact preference
    for _, name in devices:
        if "rp2040" in name.lower():
            return name
    # 2) generic USB camera
    for _, name in devices:
        low = name.lower()
        if ("usb" in low or "camera" in low) and "facetime" not in low:
            return name
    # 3) any non-FaceTime, non-screen video device
    for _, name in devices:
        low = name.lower()
        if "facetime" not in low and "screen" not in low and "capture" not in low:
            return name
    return None


def rotate_ccw(frame, deg):
    """Rotate counter-clockwise (left) by deg degrees (0/90/180/270).

    90 -> rotate left (CCW), 270 -> rotate right (CW).
    """
    if deg == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if deg == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    return frame


def main():
    argv = sys.argv[1:]
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        print("ffmpeg not found; install it or set FFMPEG path in this script.")
        sys.exit(1)

    if "--list" in argv:
        for idx, name in list_devices(ffmpeg):
            print(f"[{idx}] {name}")
        return

    scale = 2
    rotate = 90  # CCW (left); 0 disables
    width, height = 320, 240
    framerate = 10  # max the UVC descriptor advertises for 320x240
    positional = [a for a in argv if not a.startswith("-")]
    if positional:
        scale = int(positional[0])
    if "--rotate" in argv:
        rotate = int(argv[argv.index("--rotate") + 1]) % 360
    if "--width" in argv:
        width = int(argv[argv.index("--width") + 1])
    if "--height" in argv:
        height = int(argv[argv.index("--height") + 1])
    if "--fps" in argv:
        framerate = int(argv[argv.index("--fps") + 1])

    dev = find_camera_device(ffmpeg)
    if dev is None:
        print("No UVC camera found. Devices:")
        for idx, name in list_devices(ffmpeg):
            print(f"  [{idx}] {name}")
        sys.exit(1)

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation",
        "-video_size", f"{width}x{height}",
        "-framerate", str(framerate),
        "-i", dev,
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "pipe:1",
    ]
    rot_desc = f", rotate {rotate} deg CCW" if rotate else ", no rotation"
    print(f"Opening '{dev}' via ffmpeg ({width}x{height}@{framerate} fps{rot_desc})")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_bytes = width * height * 3

    saved = 0
    frames = 0
    fps = 0.0
    t0 = time.time()
    display_name = f"OV7670 Live (UVC) - {dev}"

    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if not raw or len(raw) < frame_bytes:
                # EOF or truncated -> device dropped
                err = proc.stderr.read() if proc.stderr else b""
                if err:
                    print("ffmpeg:", err.decode(errors="replace").strip())
                print("stream ended; device disconnected?")
                break
            frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3)

            if scale != 1:
                frame = cv2.resize(frame, (width * scale, height * scale),
                                   interpolation=cv2.INTER_NEAREST)
            frame = rotate_ccw(frame, rotate)

            frames += 1
            now = time.time()
            if now - t0 >= 1.0:
                fps = frames / (now - t0)
                frames = 0
                t0 = now
            cv2.putText(frame, f"{fps:.1f} FPS", (8, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
            cv2.imshow(display_name, frame)

            k = cv2.waitKey(1) & 0xFF
            if k in (ord("q"), 27):
                break
            elif k == ord("s"):
                fname = f"live_uvc_{saved:03d}.bmp"
                cv2.imwrite(fname, frame)
                print(f"  saved {fname}")
                saved += 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
