#!/usr/bin/env python3
"""Bayer 去马赛克 (bayer_demosaic.py)

纯软件实现 OV7670 Raw Bayer (BGGR/RGGB) 的双线性去马赛克与 BMP 输出。

- 双线性 G-first: G 通道先在 R/B 位置用正交 G 邻域平均插值; R/B 在缺失位置
  用同色 8-邻域平均 (对 RGGB 退化为标准几何: G 位置=共线 2 邻域, B/R 位置=
  对角 4 邻域), 并用局部 G 比率做色彩校正 (G-corrected)。
- 边缘/角点只用界内同色采样 (valid-mask), 无 padding。
- nearest_neighbor_demosaic: 确定性 2x2 块复制 (行主序 tie-break), 快速预览/
  硬件取证。
- rgb_to_bmp: 24bpp BGR bottom-up, 54B 头, 行填充 (仿 capture.py::rgb565_to_bmp)。

模块级无 sys.argv 解析 (CLI 全在 main(), __main__ 守卫) -> 可被 unittest import。

用法:
  python3 bayer_demosaic.py in.raw --width 640 --height 480 --pattern RGGB -o out.bmp
  python3 bayer_demosaic.py in.raw --width 640 --height 240 --nearest -o out.bmp
"""

import argparse
import struct

import numpy as np

DEFAULT_PATTERN = "RGGB"  # spec §6.1: 偶行偶列=R, 偶行奇列=G, 奇行偶列=G, 奇行奇列=B


def cfa_color(pattern, y, x):
    """(y,x) 处色彩: pattern 按行主序覆盖 2x2 块。"""
    return pattern[(y & 1) * 2 + (x & 1)]


def _color_map(pattern, h, w):
    """(h, w) 字符数组: 每个像素的颜色 ('R'/'G'/'B')。"""
    yy = np.arange(h)[:, None] & 1
    xx = np.arange(w)[None, :] & 1
    return np.array(list(pattern))[yy * 2 + xx]


def _valid_mask(h, w, dy, dx):
    """True where (y+dy, x+dx) 在界内 (避免 np.roll 的环绕贡献)。"""
    y = np.arange(h)[:, None]
    x = np.arange(w)[None, :]
    return (y + dy >= 0) & (y + dy < h) & (x + dx >= 0) & (x + dx < w)


def _gather_avg(src, h, w, target_mask, kernel):
    """每个像素: 在 kernel 偏移的邻居里, 对 target_mask 为真的位置,
    用 src 的值做有效掩码平均 (无有效邻居时返回 0)。"""
    vals = np.zeros((h, w), dtype=np.float64)
    cnt = np.zeros((h, w), dtype=np.float64)
    for (dy, dx) in kernel:
        shifted_src = np.roll(src, (-dy, -dx), axis=(0, 1))
        shifted_tgt = np.roll(target_mask, (-dy, -dx), axis=(0, 1))
        take = shifted_tgt & _valid_mask(h, w, dy, dx)
        vals += np.where(take, shifted_src, 0.0)
        cnt += take
    return vals / np.maximum(cnt, 1.0)


def demosaic_bayer(cfa, pattern=DEFAULT_PATTERN):
    """双线性去马赛克 (G-first + G 校正 R/B), 返回 (h,w,3) uint8 RGB。

    cfa: 2D uint8 (h, w)。边界像素只用界内同色邻居 (valid-mask)。
    对线性/常量 CFA 数学精确 (测试向量可逐像素断言)。
    """
    h, w = cfa.shape
    cfa_f = cfa.astype(np.float64)
    colormap = _color_map(pattern, h, w)
    is_g = colormap == "G"
    is_r = colormap == "R"
    is_b = colormap == "B"

    kernel8 = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
               if not (dy == 0 and dx == 0)]

    # G: R/B 位置 = 同色 (正交) G 邻域平均
    g_avg = _gather_avg(cfa_f, h, w, is_g, kernel8)
    g = np.where(is_g, cfa_f, g_avg)

    # R: 缺失位置 = 同色邻域平均 * (G_here / G_at_R_neighbors) 校正
    r_avg = _gather_avg(cfa_f, h, w, is_r, kernel8)
    g_at_r = _gather_avg(g, h, w, is_r, kernel8)
    r = np.where(is_r, cfa_f, r_avg * (g / np.maximum(g_at_r, 1e-9)))

    # B: 对称
    b_avg = _gather_avg(cfa_f, h, w, is_b, kernel8)
    g_at_b = _gather_avg(g, h, w, is_b, kernel8)
    b = np.where(is_b, cfa_f, b_avg * (g / np.maximum(g_at_b, 1e-9)))

    rgb = np.stack([r, g, b], axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def nearest_neighbor_demosaic(cfa, pattern=DEFAULT_PATTERN):
    """确定性 2x2 块复制去马赛克, 返回 (h,w,3) uint8 RGB。

    out[y,x] = (cfa[by,bx], cfa[by,bx+1], cfa[by+1,bx+1]), by=y&~1, bx=x&~1
    (块内行主序 tie-break)。要求 h,w 均为偶数 (Bayer 2x2 块)。pattern 仅用于
    校验块布局; 实际取值按 RGGB 块几何。
    """
    h, w = cfa.shape
    if h % 2 != 0 or w % 2 != 0:
        raise ValueError("nearest_neighbor_demosaic requires even h and w")
    by = np.arange(h) & ~1
    bx = np.arange(w) & ~1
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:, :, 0] = cfa[by, :][:, bx]
    rgb[:, :, 1] = cfa[by, :][:, bx + 1]
    rgb[:, :, 2] = cfa[by + 1, :][:, bx + 1]
    return rgb


def rgb_to_bmp(w, h, rgb):
    """24bpp BGR bottom-up BMP (仿 capture.py::rgb565_to_bmp 约定)。

    rgb: (h, w, 3) uint8, RGB 顺序; 输出字节序 BGR, 行自底向上, 行填充
    row_size=(w*3+3)&~3, 54B 头, BI_RGB 无压缩。
    """
    row_size = (w * 3 + 3) & ~3
    data_size = row_size * h
    header = struct.pack("<2sIHHI", b"BM", 54 + data_size, 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, data_size, 0, 0, 0, 0)
    body = bytearray()
    for y in range(h - 1, -1, -1):
        bgr = np.flip(rgb[y], axis=1).astype(np.uint8)
        body += bgr.tobytes()
        body += b"\x00" * (row_size - w * 3)
    return header + bytes(body)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Bayer CFA -> RGB 去马赛克 / BMP 输出 (8-bit raw 输入)")
    p.add_argument("input", help="raw 8-bit Bayer 文件 (w*h 字节)")
    p.add_argument("--width", type=int, required=True, help="帧宽")
    p.add_argument("--height", type=int, required=True, help="帧高")
    p.add_argument("--pattern", default=DEFAULT_PATTERN,
                   choices=["RGGB", "BGGR", "GRBG", "GBRG"],
                   help="CFA 模式 (默认 %(default)s)")
    p.add_argument("--nearest", action="store_true",
                   help="用最近邻 (块复制) 而非双线性")
    p.add_argument("-o", "--output", required=True, help="输出 .bmp")
    args = p.parse_args(argv)

    with open(args.input, "rb") as f:
        data = f.read()
    need = args.width * args.height
    if len(data) < need:
        p.error(f"{args.input}: {len(data)} B < 需要 {need} B "
                f"({args.width}x{args.height})")
    cfa = np.frombuffer(data[:need], dtype=np.uint8).reshape(args.height, args.width)
    if args.nearest:
        rgb = nearest_neighbor_demosaic(cfa, args.pattern)
    else:
        rgb = demosaic_bayer(cfa, args.pattern)
    bmp = rgb_to_bmp(args.width, args.height, rgb)
    with open(args.output, "wb") as f:
        f.write(bmp)
    print(f"{args.output}: {args.width}x{args.height} {args.pattern} "
          f"{'nearest' if args.nearest else 'bilinear'} OK ({len(bmp)} B)")


if __name__ == "__main__":
    main()
