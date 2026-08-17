#!/usr/bin/env python3
"""decode_hypo.py — 直接对比候选 6-bit 解码假设 (旧 g_map vs 新 b2-MSB) + bit1/bit2 关系

假设:
  gmap_old: g = 128*b1 + 64*b7 + 32*b6 + 16*b5 + 8*b4 + 4*b3   (旧帧, max=252)
  gmap_new: g = 128*b2 + 64*b7 + 32*b6 + 16*b5 + 8*b4 + 4*b3   (新帧候选, max=252)

对每个帧输出各假设的: 取值范围/取值数/colLag1/colLag2/diff/MAD/连续性,
并检验 bit1 与 bit2 的互斥性与位移相关 (bit1[x]==bit2[x-1]? 等).
用法: python3 decode_hypo.py <old.raw> <new.raw>
"""

import sys

import numpy as np


def load_raw(path):
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 153600:
        return data.reshape(240, 640)
    if data.size == 307200:
        return data.reshape(480, 640)
    raise ValueError(f"unexpected size {data.size}")


def decode(v, order):
    """order: list of 6 bit positions; weights [128,64,32,16,8,4]."""
    v = v.astype(np.uint16)
    out = np.zeros(v.shape, dtype=np.uint16)
    for j, b in enumerate(order):
        out += [128, 64, 32, 16, 8, 4][j] * ((v >> b) & 1)
    return out


def colcorr(a):
    a = a.astype(np.float64)
    r1 = np.corrcoef(a[:, :-1].ravel(), a[:, 1:].ravel())[0, 1]
    r2 = np.corrcoef(a[:, :-2].ravel(), a[:, 2:].ravel())[0, 1]
    return float(r1), float(r2)


def mad(a):
    d1 = np.abs(a[:, 1:].astype(np.int32) - a[:, :-1].astype(np.int32)).mean()
    d2 = np.abs(a[:, 2:].astype(np.int32) - a[:, :-2].astype(np.int32)).mean()
    return float(d1), float(d2)


def continuity(a):
    return float((np.abs(a[:, 2:].astype(np.int32) - a[:, :-2].astype(np.int32)) <= 20).mean())


def analyze(path, label):
    v = load_raw(path)
    h, w = v.shape
    print(f"\n===== {label} ({h}x{w}) =====")
    b1 = ((v >> 1) & 1).astype(np.uint8)
    b2 = ((v >> 2) & 1).astype(np.uint8)
    # bit1/bit2 互斥性
    both = ((b1 == 1) & (b2 == 1)).mean()
    p_eq = (b1 == b2).mean()
    p1, p2 = b1.mean(), b2.mean()
    print(f"bit1 ones={p1:.4f} bit2 ones={p2:.4f} P(bit1==bit2)={p_eq:.4f} P(bit1&bit2=1)={both:.4f}")
    # 位移相关: bit1[x] vs bit2[x-1], bit1[x] vs bit2[x+1], bit1 vs bit2 同行右移
    for name, a, b in [
        ("bit1[x] vs bit2[x-1]", b1[:, 1:], b2[:, :-1]),
        ("bit1[x] vs bit2[x+1]", b1[:, :-1], b2[:, 1:]),
        ("bit1[y] vs bit2[y-1]", b1[1:, :], b2[:-1, :]),
    ]:
        c = np.corrcoef(a.ravel().astype(float), b.ravel().astype(float))[0, 1]
        print(f"  {name}: corr={c:.4f} P(eq)={np.mean(a==b):.4f}")

    orders = {
        "gmap_old (1,7,6,5,4,3)": [1, 7, 6, 5, 4, 3],
        "gmap_new (2,7,6,5,4,3)": [2, 7, 6, 5, 4, 3],
        "exh_top (0,7,5,6,4,3)": [0, 7, 5, 6, 4, 3],
        "raw byte": None,
    }
    for name, order in orders.items():
        if order is None:
            g = v
        else:
            g = decode(v, order)
        r1, r2 = colcorr(g)
        m1, m2 = mad(g)
        cnt = len(np.unique(g))
        print(
            f"{name:28s} range=[{g.min()},{g.max()}] nval={cnt:3d} "
            f"colLag1={r1:+.4f} colLag2={r2:+.4f} diff={r2-r1:+.4f} "
            f"MAD1={m1:6.1f} MAD2={m2:6.1f} cont20={continuity(g):.4f}"
        )
    return v


if __name__ == "__main__":
    old = analyze(sys.argv[1], "OLD " + sys.argv[1])
    new = analyze(sys.argv[2], "NEW " + sys.argv[2])
