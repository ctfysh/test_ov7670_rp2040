#!/usr/bin/env python3
"""Shared helpers for the YD-RP2040 UVC wedge regression tests (macOS).

Single source of truth for: finding the Pico's CDC serial port, sending the
firmware's diagnostic commands ('Q' / 'V') and parsing the DBG1 replies.

The firmware replies to every diagnostic with the 4-byte magic "DBG1"
followed by one marker byte identifying the command, then a payload:
    'Q' -> 0xF9 flags ...      (pipeline state, see parse_q)
    'V' -> 0xF7 commit(4 BE) power_mode(4 BE)   (see parse_v)

Serial port discovery: the newest /dev/cu.usbmodem* device. The Pico
re-enumerates (and the port briefly disappears) after every PID rotation,
so callers pass retries and the helpers re-open the port each query.
"""
import glob
import os
import serial
import sys
import time

DBG_MAGIC = b"DBG1"
Q_MARKER = 0xF9  # pipeline state
V_MARKER = 0xF7  # UVC control counters

BAUD = 115200


def find_port():
    """Return the newest /dev/cu.usbmodem* device (the Pico), or None."""
    cands = sorted(glob.glob("/dev/cu.usbmodem*"), key=os.path.getmtime, reverse=True)
    return cands[0] if cands else None


def query(cmd, marker, port=None, timeout=0.4, retries=8):
    """Send one diagnostic command byte, return the full DBG1 reply bytes.

    Retries with a 1 s gap because the port may not be ready right after a
    re-enumeration (macOS re-probes every new USB identity on replug).
    Exits with a readable message when no valid reply arrives.
    """
    port = port or find_port()
    if port is None:
        sys.exit("no serial port found - is the Pico plugged in?")
    for _ in range(retries):
        try:
            s = serial.Serial(port, BAUD, timeout=1)
            s.reset_input_buffer()
            s.write(cmd)
            time.sleep(timeout)
            d = s.read(128)
            s.close()
        except serial.SerialException:
            time.sleep(1)
            continue
        if len(d) >= 5 and d[:4] == DBG_MAGIC and d[4] == marker:
            return d
        time.sleep(1)
    sys.exit(f"no DBG1 reply for {cmd!r} on {port} - is the fix firmware flashed?")


def parse_q(d):
    """Parse a 'Q' reply. Flags byte: bit0=fr bit1=dma bit2=tx bit3=str bit4=cam.

    Streaming is live when str==1; cam==1 means the OV7670 reports VSYNC.
    """
    f = d[5]
    return {
        "fr": (f >> 0) & 1,
        "dma": (f >> 1) & 1,
        "tx": (f >> 2) & 1,
        "str": (f >> 3) & 1,
        "cam": (f >> 4) & 1,
    }


def parse_v(d):
    """Parse a 'V' reply: commit(4 BE) + power_mode(4 BE)."""
    return {
        "commit": int.from_bytes(d[5:9], "big"),
        "power_mode": int.from_bytes(d[9:13], "big"),
    }
