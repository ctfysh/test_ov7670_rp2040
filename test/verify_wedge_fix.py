#!/usr/bin/env python3
"""One-shot regression for the macOS Photo Booth wedge fix.

The bug (proven in experiments A/B/C): macOS cameracaptured wedges a USB
camera identity keyed by (VID, PID). A wedged reopen sends ZERO UVC control
requests (commit stays 0, silent), and Photo Booth shows a black frame. The
fix rotates the PID on every session end (dead-transfer watchdog in
main.cpp), so the next open presents a fresh identity that was never wedged.

This script proves the fix end to end on the live hardware:

  Phase 0  baseline        - read commit counter and PID
  Phase 1  first session   - open PB, switch camera to YD RP2040, confirm
                             str==1 and commit grows (stream works)
  Phase 2  wedge scenario  - quit PB, WAIT for the watchdog to rotate the
                             PID (macOS re-probes each new identity and the
                             cascade settles after a few seconds), reopen PB,
                             switch camera again
  Phase 3  verdict         - str==1 AND commit grew again AND the PB window
                             is not black (screenshot pixel analysis)

PASS = the reopen streamed on a fresh identity (fix holds).
FAIL = one of the three checks failed; the script says which.

Environment: macOS, Photo Booth, Pico running the fix firmware, OV7670
connected. Needs: python3 + pyserial + Pillow (PIL).

Usage: verify_wedge_fix.py [PORT]
"""
import subprocess
import sys
import time

from pb_common import find_port, parse_q, parse_v, query

HERE = __file__.rsplit("/", 1)[0]
SWITCH = f"{HERE}/pb_switch_cam.sh"
WINID = f"{HERE}/winid.swift"

# Window crop: first-line output of winid.swift is "X Y W H" of the PB window.
WINDOW_FALLBACK = (429, 71, 720, 568)  # observed layout, used if winid fails


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def pb_running():
    return sh('pgrep -x "Photo Booth"').returncode == 0


def pb_open():
    return sh('open -a "Photo Booth"').returncode == 0


def pb_quit():
    return sh('osascript -e \'tell application "Photo Booth" to quit\'').returncode == 0


def switch_cam(tries=2):
    """Click the YD RP2040 menu item. First attempt may bounce back to
    FaceTime while macOS is still probing the new PID, so retry."""
    for i in range(tries):
        sh(f'bash {SWITCH}')
        time.sleep(1.5)
    return True


def current_pid():
    """Current USB PID of the YD RP2040 via system_profiler, or None.

    Parses the SPUSBDataType tree: take the text after the "YD RP2040"
    device-name block and read its "Product ID:" line.
    """
    out = sh("system_profiler SPUSBDataType").stdout
    blocks = out.split("YD RP2040")
    if len(blocks) < 2:
        return None
    for line in blocks[1].splitlines():
        line = line.strip()
        if line.startswith("Product ID:"):
            return line.split(":", 1)[1].strip()
    return None


def wait_pid_rotation(before, timeout=40):
    """Wait until the PID changes from `before` and then stays stable for
    8 s (macOS probe cascade settling). Returns the new PID or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = current_pid()
        if pid and pid != before:
            # require stability: same PID for 8 consecutive seconds
            stable_from = time.time()
            while time.time() - stable_from < 8:
                time.sleep(1.5)
                now_pid = current_pid()
                if now_pid != pid:
                    break  # still cascading, keep outer loop
            else:
                return pid
        time.sleep(1)
    return None


def wait_str(port, want=1, timeout=20):
    """Poll 'Q' until str==want. Returns the last state dict."""
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = parse_q(query(b"Q", 0xF9, port))
        if st["str"] == want:
            return st
        time.sleep(1)
    return st


def window_bounds():
    """(x, y, w, h) of the PB window; falls back on error."""
    out = sh(f"swift {WINID}").stdout.strip().splitlines()
    if out:
        try:
            x, y, w, h = (int(v) for v in out[0].split())
            return (x, y, w, h)
        except ValueError:
            pass
    return WINDOW_FALLBACK


def shot(path, bounds):
    x, y, w, h = bounds
    return sh(f"screencapture -x -R {x},{y},{w},{h} {path}").returncode == 0


def analyze(path):
    """(mean_luma, colorful_fraction) for the screenshot."""
    from PIL import Image
    img = Image.open(path).convert("RGB")
    px = img.load()
    w, h = img.size
    total = luma_sum = colorful = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            r, g, b = px[x, y]
            total += 1
            luma_sum += (r + g + b) / 3.0
            mx, mn = max(r, g, b), min(r, g, b)
            if mx - mn > 24:  # saturation proxy (no colorsys needed)
                colorful += 1
    mean_luma = luma_sum / total if total else 0.0
    colorful_frac = colorful / total if total else 0.0
    return mean_luma, colorful_frac


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    port = port or find_port()
    if port is None:
        print("FAIL: no serial port found - is the Pico plugged in?")
        return 1
    print(f"port: {port}")

    # ---- Phase 0: baseline ----
    v0 = parse_v(query(b"V", 0xF7, port))
    pid0 = current_pid()
    print(f"[0/3] baseline: commit={v0['commit']} PID={pid0}")

    # ---- Phase 1: first session streams ----
    if not pb_running():
        pb_open()
        time.sleep(3)
    switch_cam()
    st1 = wait_str(port, 1)
    v1 = parse_v(query(b"V", 0xF7, port))
    if st1 is None or st1["str"] != 1:
        print(f"FAIL: first session never streamed (str={st1 and st1['str']})")
        return 1
    if v1["commit"] <= v0["commit"]:
        print(f"FAIL: commit did not grow in first session ({v0['commit']} -> {v1['commit']})")
        return 1
    print(f"[1/3] first session streams: str=1 commit={v1['commit']} (was {v0['commit']})")

    # ---- Phase 2: the wedge scenario ----
    if not pb_quit():
        print("FAIL: could not quit Photo Booth")
        return 1
    print("[2/3] PB quit; waiting for PID rotation (watchdog ~7s + macOS probe cascade)...")
    pid1 = wait_pid_rotation(pid0)
    if pid1 is None:
        print(f"FAIL: PID never rotated (still {current_pid()}) - watchdog did not fire?")
        return 1
    print(f"      PID rotated: {pid0} -> {pid1}")

    pb_open()
    time.sleep(3)
    switch_cam()
    st2 = wait_str(port, 1)
    v2 = parse_v(query(b"V", 0xF7, port))
    if st2 is None or st2["str"] != 1:
        print(f"FAIL: reopen never streamed (str={st2 and st2['str']}) - wedge NOT cleared")
        return 1
    if v2["commit"] <= v1["commit"]:
        print(f"FAIL: commit did not grow on reopen ({v1['commit']} -> {v2['commit']}) - wedge NOT cleared")
        return 1
    print(f"      reopen streams on fresh PID: str=1 commit={v2['commit']} (was {v1['commit']})")

    # ---- Phase 3: not black ----
    bounds = window_bounds()
    shot_path = "/tmp/wedge_verify.png"
    shot(bounds, shot_path)
    mean_luma, colorful = analyze(shot_path)
    if mean_luma < 15:
        print(f"FAIL: PB window is black (mean_luma={mean_luma:.1f})")
        return 1
    if colorful < 0.05:
        print(f"WARN: window has little color ({colorful:.0%}) - camera may be FaceTime, not YD")
    print(f"[3/3] PB window live: mean_luma={mean_luma:.1f} colorful={colorful:.0%} ({shot_path})")

    print("PASS: Photo Booth reopen streams on a rotated PID - wedge fix holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
