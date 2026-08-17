#!/usr/bin/env python3
"""sweep_frames.py — 对所有新旧帧跑两个解码假设, 输出一致性表.

判断规则 (依据文档 §7.9 + 新发现):
  - colLag1 < 0  → 棋盘格伪影 (解码错误信号)
  - 好的解码: colLag2 高 (0.9+), colLag1 > 0, MAD2 低, cont20 高
用法: python3 sweep_frames.py <old_dir> <new_dir>
"""

import glob
import sys

import numpy as np

sys.path.insert(0, ".")
from decode_hypo import load_raw, decode, colcorr, mad, continuity


def evaluate(v, order):
    g = decode(v, order)
    r1, r2 = colcorr(g)
    m1, m2 = mad(g)
    c = continuity(g)
    nval = len(np.unique(g))
    return dict(r1=r1, r2=r2, m1=m1, m2=m2, cont=c, nval=nval, rng=(int(g.min()), int(g.max())))


OLD_ORD = [1, 7, 6, 5, 4, 3]
NEW_ORD = [2, 7, 6, 5, 4, 3]


def row(path):
    v = load_raw(path)
    o = evaluate(v, OLD_ORD)
    n = evaluate(v, NEW_ORD)
    # 判定: gmap_new 若 colLag1>0 且 colLag2 更高且 MAD 更低 → new wins
    old_wins = (o["r1"] > 0) and (o["r2"] > n["r2"] * 0.995) and (o["m2"] < n["m2"] * 1.01)
    new_wins = (n["r1"] > 0) and (n["r2"] > o["r2"] * 0.995) and (n["m2"] < o["m2"] * 1.01)
    verdict = "OLD" if old_wins and not new_wins else ("NEW" if new_wins and not old_wins else "?")
    print(
        f"{path:55s} {v.shape[0]:3d}x{v.shape[1]} "
        f"old[r1={o['r1']:+.3f} r2={o['r2']:.3f} m2={o['m2']:5.1f} c={o['cont']:.3f} n={o['nval']:3d}] "
        f"new[r1={n['r1']:+.3f} r2={n['r2']:.3f} m2={n['m2']:5.1f} c={n['cont']:.3f} n={n['nval']:3d}] "
        f"=> {verdict}"
    )
    return verdict


if __name__ == "__main__":
    print(f"{'frame':55s} {'size':9s}  verdict")
    old_files = sorted(glob.glob(sys.argv[1] + "/*.raw"))
    new_files = sorted(glob.glob(sys.argv[2] + "/*.raw"))
    for f in old_files:
        row(f)
    for f in new_files:
        row(f)
