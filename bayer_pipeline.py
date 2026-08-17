#!/usr/bin/env python3
"""OV7670 Raw Bayer 处理管线 (bayer_pipeline.py)

把本项目实验得到的完整取证结论固化为可复用的纯函数管线:
  g_map                  : 6-bit 位序解码 (b1=MSB, 序 (1,7,6,5,4,3)) -> 0..252
  fix_dead_pixels        : 散点死点修复 (同色 4-邻域中值, 差>阈 替换)
  fix_defect_region      : 楔形区域缺陷修复 (带外同相位参考插值, 逐像素判定)
  render                 : 去马赛克 + WB + 伽马 -> PNG

取证结论 (docs/ 记录, 本文件固化):
- 位序: 原始字节 v=129*b7+64*b6+32*b5+16*b4+8*b3+2*b1, b0==b7, b2≈噪声;
  g(v)=128*b1+64*b7+32*b6+16*b5+8*b4+4*b3, b1 为真 MSB (空间相关 0.863 最高、
  空间连续性 0.670 最高、邻列 MAD 42.7 < 原始字节 70.4)。
- 240x320 模式: every-2nd-PCLK 采集, 每行 320 字节, RGGB 拜耳格式。
- 白平衡: 灰度世界 R/B 增益。
- 伽马: 0.85 提亮。

CLI 在 main() 内, __main__ 守卫 -> 纯函数层可被 unittest import。
"""

import argparse

import numpy as np

from bayer_demosaic import demosaic_bayer


def fix_dead_pixels(cfa, threshold=60.0, skip=None):
    """散点死点修复: 与同色 4-邻域 (上下左右, 间隔 2) 中值差 > threshold 则替换。

    skip: (h,w) bool mask, True 的像素不处理 (如区域缺陷修复区)。
    返回 (修复后的副本, 替换像素数)。
    """
    cfa = cfa.astype(np.float64).copy()
    h, w = cfa.shape
    if skip is None:
        skip = np.zeros((h, w), dtype=bool)
    count = 0
    for y in range(2, h - 2):
        for x in range(2, w - 2):
            if skip[y, x]:
                continue
            nbrs = []
            for dy, dx in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                yy, xx = y + dy, x + dx
                if 0 <= yy < h and 0 <= xx < w and not skip[yy, xx]:
                    nbrs.append(cfa[yy, xx])
            if len(nbrs) < 2:
                continue
            med = np.median(nbrs)
            if abs(cfa[y, x] - med) > threshold:
                cfa[y, x] = med
                count += 1
    return cfa, count


def fix_defect_region(cfa, region_cols, y_lo, y_hi, threshold=55.0):
    """楔形区域缺陷修复: 区域内像素与"带外同相位参考"差 > threshold 则替换。

    带外参考 = 左右最近非缺陷列 (同相位, x±2 步进) 的均值; 关键是从缺陷带外
    取参考, 避免缺陷像素互相污染。区域外像素原样保留。

    region_cols: 疑似缺陷列集合 (如 range(502, 517))。
    y_lo, y_hi: 检查行范围 (半开区间 [y_lo, y_hi))。
    返回 (修复后的副本, 替换掩码, 替换像素数)。
    """
    cfa = cfa.astype(np.float64).copy()
    h, w = cfa.shape
    replaced = np.zeros((h, w), dtype=bool)
    count = 0
    for y in range(y_lo, min(y_hi, h)):
        for x in range(w):
            if x not in region_cols:
                continue
            lv = rv = None
            lx = x - 2
            while lx >= 0:
                if lx not in region_cols:
                    lv = cfa[y, lx]
                    break
                lx -= 2
            rx = x + 2
            while rx < w:
                if rx not in region_cols:
                    rv = cfa[y, rx]
                    break
                rx += 2
            refs = [v for v in (lv, rv) if v is not None]
            if not refs:
                continue
            ref = np.mean(refs)
            if abs(cfa[y, x] - ref) > threshold:
                cfa[y, x] = ref
                replaced[y, x] = True
                count += 1
    return cfa, replaced, count


def render(cfa, pattern="RGGB", r_gain=None, b_gain=None, gamma=0.85, b_extra=0.93):
    """去马赛克 -> 灰度世界 WB -> 伽马, 返回 (h,w,3) uint8 RGB。

    cfa: 240x320 CFA (uint8 或 float64)。r_gain/b_gain 缺省时按全帧灰度世界
    自动计算 (R/B 增益使 R/G=B/G=1)。b_extra: 用户偏好的额外降蓝系数。
    """
    rgb = demosaic_bayer(cfa.astype(np.uint8), pattern).astype(np.float64)
    if r_gain is None:
        r_gain = rgb[:, :, 1].mean() / rgb[:, :, 0].mean()
    if b_gain is None:
        b_gain = rgb[:, :, 1].mean() / rgb[:, :, 2].mean()
    img = rgb.copy()
    img[:, :, 0] *= r_gain
    img[:, :, 2] *= b_gain * b_extra
    img = np.clip(img, 0, 255)
    img = 255 * (img / 255) ** gamma
    return img.astype(np.uint8)


def fix_leading_zeros(cfa):
    """修复每行前2个零字节 (Bayer pipeline delay): 用第2列数据填充前2列。

    OV7670 raw 模式每行前2字节始终为零 (传感器 pipeline delay)，
    不处理会导致图像横向拉伸 + 零值污染 demosaic/WB。
    返回修复后的副本 (原矩阵不变)。
    """
    cfa = cfa.copy()
    cfa[:, 0] = cfa[:, 2]
    cfa[:, 1] = cfa[:, 3]
    return cfa


def pipeline(raw, dead_threshold=60.0, fix_dead=True, pattern="RGGB",
             r_gain=None, b_gain=None, gamma=0.85, b_extra=0.93):
    """240x320 raw Bayer -> RGB: 修复零列 -> 散点死点修复 -> 去马赛克 -> WB -> 伽马。"""
    cfa = fix_leading_zeros(raw.astype(np.float64))
    stats = {}
    if fix_dead:
        cfa, stats["dead_replaced"] = fix_dead_pixels(cfa, dead_threshold)
    rgb = render(cfa, pattern, r_gain, b_gain, gamma, b_extra)
    return rgb, stats


def main(argv=None):
    p = argparse.ArgumentParser(
        description="OV7670 raw Bayer 320x240 管线: 死点修复 + 去马赛克 + 渲染 PNG")
    p.add_argument("input", help="240x320 raw 字节文件")
    p.add_argument("-o", "--output", required=True, help="输出 PNG")
    p.add_argument("--no-dead-fix", action="store_true",
                   help="跳过散点死点修复")
    p.add_argument("--dead-threshold", type=float, default=60.0,
                   help="死点判定阈值 (默认 %(default)s)")
    p.add_argument("--pattern", default="RGGB", help="CFA 模式 (默认 %(default)s)")
    p.add_argument("--r-gain", type=float, default=None,
                   help="R 增益 (默认全帧灰度世界)")
    p.add_argument("--b-gain", type=float, default=None,
                   help="B 增益 (默认全帧灰度世界)")
    p.add_argument("--b-extra", type=float, default=0.93,
                   help="额外降蓝系数 (默认 %(default)s)")
    p.add_argument("--gamma", type=float, default=0.85,
                   help="伽马 (默认 %(default)s)")
    args = p.parse_args(argv)

    raw = np.fromfile(args.input, dtype=np.uint8).reshape(240, 320)
    rgb, stats = pipeline(
        raw, dead_threshold=args.dead_threshold,
        fix_dead=not args.no_dead_fix,
        pattern=args.pattern,
        r_gain=args.r_gain, b_gain=args.b_gain,
        gamma=args.gamma, b_extra=args.b_extra)

    from PIL import Image
    Image.fromarray(rgb).save(args.output)
    print(f"{args.output}: {rgb.shape[1]}x{rgb.shape[0]} saved")
    if stats:
        print(f"  dead_replaced={stats.get('dead_replaced', 0)}")


if __name__ == "__main__":
    main()
