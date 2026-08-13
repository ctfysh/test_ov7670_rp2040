#!/usr/bin/env python3
"""Query the UVC control counters: send 'V', print commit/power_mode.

commit counts VS_COMMIT requests the host sent. A wedged reopen (macOS
cameracaptured refusing a previously-wedged (VID,PID) identity) sends ZERO
commits - that silent wedge is exactly what the PID-rotation fix avoids.

Usage: pb_v_now.py [PORT]
"""
import sys

from pb_common import parse_v, query


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    d = query(b"V", 0xF7, port)
    c = parse_v(d)
    print(f"commit={c['commit']} power_mode={c['power_mode']}")


if __name__ == "__main__":
    main()
