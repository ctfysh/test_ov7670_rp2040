#!/usr/bin/env python3
"""raw_forensic.py — OV7670 raw 字节位级取证 (§7.9 同口径, 可复用)

对任意 raw 文件 (640x240 或 640x480) 计算:
  1. 逐位统计: ones 比例 + 行向 lag2 自相关 (结构分数)
  2. 位对相等矩阵 P(bit_i==bit_j)
  3. 原始字节整帧 Pearson 列相关 colLag1/colLag2 (Bayer 签名)
  4. g_map (既有 6-bit 解码) 后的 colLag1/colLag2 + 渲染 BMP
  5. 穷举 6-bit 位序搜索: 8 bits 取 6 + 全排列, 以 colLag2-colLag1 为
     Bayer 签名分数, 输出 top 位序 (与文档 b1=MSB 序 (1,7,6,5,4,3) 对比)

零依赖 (numpy + 可选 PIL)。用法:
  python3 raw_forensic.py <file.raw> [--exhaustive] [--out out.png]
"""

import argparse
import itertools
import sys

import numpy as np


def load_raw(path):
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 153600:
        return data.reshape(240, 640), (240, 640)
    if data.size == 307200:
        return data.reshape(480, 640), (480, 640)
    raise ValueError(f"unexpected raw size {data.size} (expect 153600 or 307200)")


def per_bit_stats(v):
    """返回 list of (ones_ratio, lag2_autocorr). lag2 = 行内相距 2 像素的位自相关."""
    n = v.shape[0] * v.shape[1]
    out = []
    for b in range(8):
        bit = (v >> b) & 1
        ones = bit.mean()
        # 行向 lag2 自相关: 相邻同色位点 (x 与 x+2 同相位)
        lag2 = []
        for row in range(v.shape[0]):
            a = bit[row, :-2]
            c = bit[row, 2:]
            if a.size:
                lag2.append(np.corrcoef(a.astype(float), c.astype(float))[0, 1])
        lag2 = float(np.nanmean(lag2)) if lag2 else float("nan")
        out.append((ones, lag2))
    return out


def eq_matrix(v):
    """P(bit_i == bit_j) 8x8 矩阵."""
    bits = [(v >> b) & 1 for b in range(8)]
    m = np.zeros((8, 8))
    for i in range(8):
        for j in range(8):
            m[i, j] = (bits[i] == bits[j]).mean()
    return m


def colcorr(frame):
    """整帧 Pearson 列相关: 返回 (colLag1, colLag2). frame 为 2D uint."""
    f = frame.astype(np.float64)
    r1 = np.corrcoef(f[:, :-1].ravel(), f[:, 1:].ravel())[0, 1]
    r2 = np.corrcoef(f[:, :-2].ravel(), f[:, 2:].ravel())[0, 1]
    return float(r1), float(r2)


def g_map(v):
    """既有解码: g(v)=128*b1+64*b7+32*b6+16*b5+8*b4+4*b3 (0..252)."""
    v = v.astype(np.uint16)
    b7 = (v >> 7) & 1
    b6 = (v >> 6) & 1
    b5 = (v >> 5) & 1
    b4 = (v >> 4) & 1
    b3 = (v >> 3) & 1
    b1 = (v >> 1) & 1
    return (128 * b1 + 64 * b7 + 32 * b6 + 16 * b5 + 8 * b4 + 4 * b3).astype(np.uint8)


def exhaustive_order(v, subsample=2):
    """穷举 6-bit 位序: 8 bits 取 6 (56 子集) x 6! 排列 = 40320 种.
    分数 = colLag2 - colLag1 (Bayer 签名), 行子采样加速.
    返回 top 列表: [(score, (w0..w5 bit positions, weights))...]"""
    rows = v[::subsample]  # 子采样行加速
    bits = {b: ((v >> b) & 1).astype(np.uint16) for b in range(8)}
    bits_s = {b: ((rows >> b) & 1).astype(np.uint16) for b in range(8)}
    weights = [128, 64, 32, 16, 8, 4]
    best = []
    nbits = 8
    for combo in itertools.combinations(range(nbits), 6):
        for perm in itertools.permutations(combo):
            # decoded = sum weights[j] * bit[perm[j]]
            dec = np.zeros(rows.shape, dtype=np.uint16)
            for j, b in enumerate(perm):
                dec += weights[j] * bits_s[b]
            r1, r2 = colcorr(dec)
            score = r2 - r1
            if len(best) < 15:
                best.append((score, perm))
                best.sort(key=lambda t: t[0], reverse=True)
            elif score > best[-1][0]:
                best[-1] = (score, perm)
                best.sort(key=lambda t: t[0], reverse=True)
    return best


def render_bmp(cfa, path):
    """灰度渲染 raw 为 BMP (零依赖)."""
    try:
        from PIL import Image
    except ImportError:
        return
    img = np.clip((cfa.astype(np.float64) / 252 * 255), 0, 255).astype(np.uint8)
    Image.fromarray(img).save(path)


def main(argv=None):
    p = argparse.ArgumentParser(description="raw 字节位级取证 (§7.9 同口径)")
    p.add_argument("input")
    p.add_argument("--exhaustive", action="store_true", help="穷举 6-bit 位序搜索")
    p.add_argument("--out", default=None, help="g_map 解码灰度 BMP 输出路径")
    p.add_argument("--no-gmap", action="store_true", help="跳过 g_map 解码检查")
    args = p.parse_args(argv)

    v, (h, w) = load_raw(args.input)
    print(f"== {args.input} ({h}x{w}) ==")

    print("\n[1] 逐位统计: ones, lag2 自相关")
    for b, (ones, lag2) in enumerate(per_bit_stats(v)):
        print(f"  bit{b}: ones={ones:.4f} lag2={lag2:.4f}")

    print("\n[2] 位对相等矩阵 P(bit_i==bit_j)")
    m = eq_matrix(v)
    for i in range(8):
        print("  bit%d:" % i + " ".join(f"{m[i, j]:.3f}" for j in range(8)))

    print("\n[3] 原始字节 Bayer 签名 colLag1/colLag2")
    r1, r2 = colcorr(v)
    print(f"  colLag1={r1:.4f} colLag2={r2:.4f} (diff={r2-r1:+.4f})")

    if not args.no_gmap:
        print("\n[4] g_map 解码后 Bayer 签名")
        g = g_map(v)
        r1g, r2g = colcorr(g)
        print(f"  colLag1={r1g:.4f} colLag2={r2g:.4f} (diff={r2g-r1g:+.4f})")
        if args.out:
            render_bmp(g, args.out)
            print(f"  BMP -> {args.out}")

    if args.exhaustive:
        print("\n[5] 穷举 6-bit 位序 (colLag2-colLag1 分数, 行子采样 2)")
        top = exhaustive_order(v, subsample=2)
        for score, perm in top[:8]:
            order = "->".join(f"b{i}@{w}" for i, w in zip(perm, [128, 64, 32, 16, 8, 4]))
            print(f"  score={score:+.4f}  {order}")
        # top-1 全分辨率复核
        perm = top[0][1]
        dec = np.zeros(v.shape, dtype=np.uint16)
        for j, b in enumerate(perm):
            dec += [128, 64, 32, 16, 8, 4][j] * ((v >> b) & 1).astype(np.uint16)
        r1f, r2f = colcorr(dec)
        print(f"  top-1 全分辨率: colLag1={r1f:.4f} colLag2={r2f:.4f} (diff={r2f-r1f:+.4f})")


if __name__ == "__main__":
    main()
