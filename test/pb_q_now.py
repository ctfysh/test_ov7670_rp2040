#!/usr/bin/env python3
"""Query the UVC pipeline state: send 'Q', print fr/dma/tx/str/cam.

Usage: pb_q_now.py [PORT]
"""
import sys

from pb_common import parse_q, query


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    d = query(b"Q", 0xF9, port)
    st = parse_q(d)
    print(f"fr={st['fr']} dma={st['dma']} tx={st['tx']} str={st['str']} cam={st['cam']}")


if __name__ == "__main__":
    main()
