#!/usr/bin/env python3
"""OV7670 Raw Bayer 处理管线 (bayer_pipeline.py)

把本项目实验得到的完整取证结论固化为可复用的纯函数管线:
  fix_dead_pixels        : 散点死点修复 (同色 4-邻域中值, 差>阈 替换)
  fix_defect_region      : 楔形区域缺陷修复 (带外同相位参考插值, 逐像素判定)
  render_stitched        : 上/下半窗分相位去马赛克 + 拼接 + WB + 伽马 -> PNG

解码模式:
- COM15=0xC0 (当前): 8-bit 全独立, raw 直通, 不需要 g_map。
- COM15=0xD0 (旧): 6-bit 有效 (bit0==bit7 镜像, bit2 噪声), 需要 g_map 解码。
  旧模式向后兼容: pipeline(..., use_gmap=True, map="old"|"new")。

相位: 上下窗均 BGGR (COM15=0xC0 后全帧 CFA pattern 一致)

CLI 在 main() 内, __main__ 守卫 -> 纯函数层可被 unittest import。
"""

import argparse

import numpy as np

from bayer_demosaic import demosaic_bayer


def g_map(v, map="old"):
    """6-bit 位序解码: 0..252, 4 的倍数。

    map="old" (08-14 芯片): g(v)=128*b1+64*b7+32*b6+16*b5+8*b4+4*b3, b1 为 MSB。
    map="new" (08-16 更换的芯片): g(v)=128*b2+64*b7+32*b6+16*b5+8*b4+4*b3, b2 为 MSB
      (raw 通路位映射为芯片个体差异: 换摄像头后 b2 取代 b1 成为最高位)。

    输入 uint8 ndarray (原始字节), 输出同形状 uint8。
    """
    v = v.astype(np.uint16)
    b7 = (v >> 7) & 1
    b6 = (v >> 6) & 1
    b5 = (v >> 5) & 1
    b4 = (v >> 4) & 1
    b3 = (v >> 3) & 1
    msb = ((v >> 2) & 1) if map == "new" else ((v >> 1) & 1)
    return (128 * msb + 64 * b7 + 32 * b6 + 16 * b5 + 8 * b4 + 4 * b3).astype(np.uint8)


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


def render_stitched(cfa, upper_pattern="GRBG", lower_pattern="BGGR",
                    r_gain=None, b_gain=None, gamma=0.85, b_extra=0.93):
    """上/下半窗分相位去马赛克 -> vstack 拼接 -> 灰度世界 WB -> 伽马, 返回
    (h,w,3) uint8 RGB。

    cfa: 拼接后 480x640 CFA (行 0..239 上窗, 240..479 下窗)。
    r_gain/b_gain 缺省时按全帧灰度世界自动计算 (R/B 增益使 R/G=B/G=1)。
    b_extra: 用户偏好的额外降蓝系数 (叠在灰度世界 B 增益上)。
    """
    up = demosaic_bayer(cfa[:240].astype(np.uint8), upper_pattern).astype(np.float64)
    lo = demosaic_bayer(cfa[240:].astype(np.uint8), lower_pattern).astype(np.float64)
    stitched = np.vstack([up, lo])
    if r_gain is None:
        r_gain = stitched[:, :, 1].mean() / stitched[:, :, 0].mean()
    if b_gain is None:
        b_gain = stitched[:, :, 1].mean() / stitched[:, :, 2].mean()
    img = stitched.copy()
    img[:, :, 0] *= r_gain
    img[:, :, 2] *= b_gain * b_extra
    img = np.clip(img, 0, 255)
    img = 255 * (img / 255) ** gamma
    return img.astype(np.uint8)


def pipeline(raw, use_gmap=False, map="old", region_cols=range(502, 517),
             region_y=(60, 160), dead_threshold=60.0, region_threshold=55.0,
             fix_region=True, fix_dead=True, upper_pattern="GRBG",
             lower_pattern="BGGR", r_gain=None, b_gain=None, gamma=0.85,
             b_extra=0.93):
    """完整管线: [可选 g_map] -> 楔形区域修复 -> 散点死点修复 -> 渲染。

    raw: 480x640 uint8 原始字节。
    use_gmap: True 时先做 6-bit 位序解码 (旧 COM15=0xD0 模式), False 直通。
    返回 (rgb, 修复统计 dict)。
    """
    cfa = g_map(raw, map).astype(np.float64) if use_gmap else raw.astype(np.float64)
    stats = {}
    replaced = np.zeros(cfa.shape, dtype=bool)
    # 1. 区域缺陷修复 (先于死点, 其替换区由 skip 保护)
    if fix_region:
        cfa, replaced, stats["region_replaced"] = fix_defect_region(
            cfa, region_cols, region_y[0], region_y[1], region_threshold)
        stats["region_skipped"] = int(replaced.sum())
    # 2. 散点死点修复 (跳过区域缺陷修复过的像素)
    if fix_dead:
        cfa, stats["dead_replaced"] = fix_dead_pixels(
            cfa, dead_threshold, skip=replaced)
    # 3. 渲染
    rgb = render_stitched(cfa, upper_pattern, lower_pattern,
                          r_gain, b_gain, gamma, b_extra)
    return rgb, stats


def main(argv=None):
    p = argparse.ArgumentParser(
        description="OV7670 raw Bayer 取证管线: 位序解码 + 缺陷修复 + 渲染 PNG")
    p.add_argument("input", help="480x640 raw 字节文件")
    p.add_argument("-o", "--output", required=True, help="输出 PNG")
    p.add_argument("--no-region-fix", action="store_true",
                   help="跳过楔形区域缺陷修复 (保留原始数据)")
    p.add_argument("--no-dead-fix", action="store_true",
                   help="跳过散点死点修复")
    p.add_argument("--use-gmap", action="store_true",
                   help="启用 6-bit 位序解码 (旧 COM15=0xD0 模式)")
    p.add_argument("--map", choices=["old", "new"], default="old",
                   help="位映射: old=b1 MSB (08-14 芯片), new=b2 MSB "
                        "(08-16 更换的芯片) (默认 %(default)s)")
    p.add_argument("--region-cols", default="502-516",
                   help="疑似缺陷列区间, 如 '502-516' (默认 %(default)s)")
    p.add_argument("--region-y", default="60-160",
                   help="区域修复检查行范围, 如 '60-160' (默认 %(default)s)")
    p.add_argument("--dead-threshold", type=float, default=60.0,
                   help="死点判定阈值 (默认 %(default)s)")
    p.add_argument("--region-threshold", type=float, default=55.0,
                   help="区域缺陷判定阈值 (默认 %(default)s)")
    p.add_argument("--r-gain", type=float, default=None,
                   help="R 增益 (默认全帧灰度世界)")
    p.add_argument("--b-gain", type=float, default=None,
                   help="B 增益 (默认全帧灰度世界)")
    p.add_argument("--b-extra", type=float, default=0.93,
                   help="额外降蓝系数 (默认 %(default)s)")
    p.add_argument("--gamma", type=float, default=0.85,
                   help="伽马 (默认 %(default)s)")
    args = p.parse_args(argv)

    def _parse_range(s, name):
        a, b = (int(t) for t in s.split("-"))
        if a < 0 or b < a:
            p.error(f"invalid --{name}: {s}")
        return range(a, b + 1), (a, b + 1)  # 闭区间 [a, b]

    region_cols, _ = _parse_range(args.region_cols, "region-cols")
    _, region_y = _parse_range(args.region_y, "region-y")

    raw = np.fromfile(args.input, dtype=np.uint8).reshape(480, 640)
    rgb, stats = pipeline(
        raw, use_gmap=args.use_gmap, map=args.map,
        region_cols=region_cols, region_y=region_y,
        dead_threshold=args.dead_threshold,
        region_threshold=args.region_threshold,
        fix_region=not args.no_region_fix, fix_dead=not args.no_dead_fix,
        r_gain=args.r_gain, b_gain=args.b_gain,
        gamma=args.gamma, b_extra=args.b_extra)

    from PIL import Image
    Image.fromarray(rgb).save(args.output)
    print(f"{args.output}: {rgb.shape[1]}x{rgb.shape[0]} saved")
    if stats:
        print(f"  region_replaced={stats['region_replaced']} "
              f"(skip {stats['region_skipped']}) dead_replaced={stats['dead_replaced']}")


if __name__ == "__main__":
    main()
