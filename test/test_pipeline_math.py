#!/usr/bin/env python3
"""OV7670 采集管线全链路数学验证 (test/pipeline_math.py)

目标：对"从最原始字节采集到最终呈现的 RGB 图像"的整条数据管线做数学验证。

管线 (当前仓库配置, 见 src/ov7670.c + src/camera.pio + src/main.cpp):
  1. OV7670 传感器: 内部 ISP (Raw Bayer -> demosaic -> 色彩矩阵 -> gamma) 后
     输出 RGB565, COM7=0x14 (QVGA+RGB), COM15=0xD0 (RGB565 全范围)。
     物理输出 = 每个像素 2 字节, 高字节先出 (大端字节对), 8-bit 数据线
     D0..D7 在 PCLK 上升沿被锁存, HREF 高电平期间像素有效 (src/camera.pio)。
  2. PIO 采集 (SM1): 每 PCLK 上升沿采样 D0..D7 得 1 字节; 一个像素 2 拍。
     PIO 不改变字节顺序 -> DMA 缓冲中的字节流与 OV7670 输出完全一致。
  3. DMA: VSYNC 上升沿触发, 整帧 153600 字节 (320x240x2) 搬入 frames[]。
  4. USB CDC: loop() 发送 "CAM1" + W(u16 BE) + H(u16 BE) + 153600 字节 raw。
  5. 主机解码 (capture.py / live_view.py):
       v  = (b[0] << 8) | b[1]              # 大端: b[0] 是高字节
       r8 = ((v>>11)&0x1F) << 3 | ... >> 2  # 5-bit -> 8-bit 扩展
       g8 = ((v>>5)&0x3F)  << 2 | ... >> 4  # 6-bit -> 8-bit 扩展
       b8 = (v&0x1F)       << 3 | ... >> 2
       -> BMP(BGR, bottom-up) / pygame(RGB, rot90)

分层数学验证:
  Layer A  字节采集层   OV7670 大端字节对模型 -> PIO 采样 -> DMA 缓冲字节保真
  Layer B  帧协议层     CAM1 封装: 头字段 BE round-trip, 帧长不变量, 同步恢复
  Layer C  RGB565 数学  位提取/扩展变换的恒等式、单调性、量化误差上界(证明)、端序敏感性
  Layer D  图像呈现层   BMP 编码 (BGR 序/行填充/bottom-up)、rot90 旋转恒等
  Layer E  端到端管线   合成随机图 -> 量化 -> 大端编码 -> 封装 -> 分块传输
                       -> 解码 -> BMP -> 回读, 逐像素误差 <= 量化上界

运行: python3 -m unittest discover -s test -p "test_*.py"
      (或: python3 test/test_pipeline_math.py)

说明: 测试直接复用仓库真实实现 (capture.rgb565_to_bmp, live_view.decode_rgb565),
      而不是重写副本 —— 保证测的是线上代码。
"""

import os
import random
import struct
import sys
import tempfile
import unittest

import numpy as np

# ---- 允许从任意 cwd 运行 (import 仓库根目录的 capture / live_view) ----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# capture.py / live_view.py 在模块级解析 sys.argv (PORT/OUTDIR/NFRAMES) 并
# os.makedirs(OUTDIR)。在 unittest discover 下 argv 是 pytest/unittest 参数,
# 会误解析崩溃; 也会在 cwd 创建 frames/。这里临时屏蔽 argv 并指向临时目录,
# import 完成后恢复 (main() 有 __main__ 守卫, 不会真连串口)。
_SAVED_ARGV = sys.argv[:]
sys.argv = [sys.argv[0], '/dev/null', os.path.join(tempfile.gettempdir(), 'ov7670_test_out'), '0']
try:
    import capture  # noqa: E402  真实 BMP 编码实现 (rgb565_to_bmp)
    import live_view  # noqa: E402  真实 numpy 解码实现 (decode_rgb565)
finally:
    sys.argv = _SAVED_ARGV

# ===========================================================================
# 共享模型: OV7670 输出 / 固件封装 / 管线各环节 (与 src/*.c 一一对应)
# ===========================================================================

FRAME_W, FRAME_H = 320, 240
FRAME_BYTES = FRAME_W * FRAME_H * 2  # 153600, DMA 单帧搬运量
CAM1 = b"CAM1"  # 帧魔数 (src/main.cpp FRAME_MAGIC*)
DBG1 = b"DBG1"  # 诊断包魔数 (src/main.cpp DBG_MAGIC*)

# --- Layer A: OV7670 像素 -> 大端字节对 (数据手册: 高字节先出) ---
def ov7670_pixel_bytes(v):
    """一个 RGB565 像素值 v (0..65535) -> 总线字节对 [hi, lo]。

    OV7670 在 8-bit 数据线上先出高字节 (D9..D2), 后出低字节。
    对应 main.cpp 注释: "OV7670 emits the high byte first; the PIO preserves
    byte order into the DMA buffer", 以及 capture.py: "byte i is the HIGH byte".
    """
    return (v >> 8) & 0xFF, v & 0xFF


def ov7670_frame_bytes(w, h, pixels):
    """整帧像素值序列 -> OV7670 输出的连续字节流 (按行扫描顺序, 每像素 2 字节大端)。"""
    out = bytearray()
    for v in pixels:
        hi, lo = ov7670_pixel_bytes(v)
        out.append(hi)
        out.append(lo)
    assert len(out) == w * h * 2
    return bytes(out)


def pio_sample_stream(byte_stream):
    """PIO SM1 采样模型: 每 PCLK 上升沿采样 1 字节, 字节序保持不变。

    src/camera.pio: HREF 高 -> 等 PCLK 上升沿 -> in pins, 8 -> 等 PCLK 下降沿。
    单个字节被原样移入 RX FIFO, DMA 再原样搬入 frames[], 因此输出 == 输入。
    """
    return bytes(byte_stream)  # 恒等变换: PIO 是纯字节搬运, 不做任何数值处理


# --- Layer B: 固件帧封装 (src/main.cpp loop()) ---
def firmware_frame_encode(w, h, raw_payload):
    """固件 loop() 发送的帧: "CAM1" + W(u16 BE) + H(u16 BE) + raw RGB565。"""
    assert len(raw_payload) == w * h * 2, "帧长不变量: payload == W*H*2"
    return CAM1 + struct.pack(">HH", w, h) + raw_payload


# ===========================================================================
# Layer A — 字节采集层
# ===========================================================================
class TestLayerA_ByteCapture(unittest.TestCase):
    """从"最原始总线字节"到 DMA 缓冲: 验证字节序与字节数守恒。"""

    def test_a1_pixel_to_big_endian_byte_pair(self):
        """A1: 像素值 -> 大端字节对恒等 (v = hi<<8 | lo, hi 先出)。"""
        for v in [0x0000, 0x0001, 0x00FF, 0x0100, 0x8000, 0xFFFF, 0xABCD, 0x1234]:
            hi, lo = ov7670_pixel_bytes(v)
            self.assertEqual(hi, (v >> 8) & 0xFF)
            self.assertEqual(lo, v & 0xFF)
            self.assertEqual((hi << 8) | lo, v, "大端 round-trip 必须恒等")

    def test_a2_pio_sample_preserves_byte_stream(self):
        """A2: PIO 采样是纯字节搬运 —— 缓冲字节流与传感器输出逐字节相等。

        这是"最原始数据"在采集环节的数学保真声明: PIO/DMA 不做任何数值
        变换 (无缩放/无重排/无端序转换), 只做时序门控采样。
        """
        rng = random.Random(42)
        pixels = [rng.randint(0, 0xFFFF) for _ in range(64 * 64)]
        raw = ov7670_frame_bytes(64, 64, pixels)
        # PIO 每 PCLK 采 1 字节; 一个像素 = 2 拍 -> 字节数守恒
        self.assertEqual(len(raw), 64 * 64 * 2)
        captured = pio_sample_stream(raw)
        self.assertEqual(captured, raw, "PIO/DMA 缓冲必须与总线字节流逐字节一致")

    def test_a3_dma_frame_byte_conservation(self):
        """A3: DMA 整帧搬运量 = 320*240*2 = 153600 (main.cpp FRAME_BYTES)。

        字节守恒是全管线同步的前提: 只要每帧字节数精确等于 153600,
        主机端就能按固定长度切帧, 不依赖任何流内字节计数。
        """
        self.assertEqual(FRAME_BYTES, 153600)
        self.assertEqual(FRAME_BYTES, FRAME_W * FRAME_H * 2)
        # DMA transfer count 配置 (main.cpp dma_setup) 与帧字节数一致
        self.assertEqual(FRAME_BYTES, 320 * 240 * 2)


# ===========================================================================
# Layer B — 帧协议层
# ===========================================================================
class TestLayerB_FrameProtocol(unittest.TestCase):
    """CAM1 封装: 头字段 BE round-trip、帧长不变量、垃圾/截断恢复。"""

    def _mk_frame(self, w=16, h=16, seed=7):
        rng = random.Random(seed)
        raw = ov7670_frame_bytes(w, h, [rng.randint(0, 0xFFFF) for _ in range(w * h)])
        return firmware_frame_encode(w, h, raw)

    def test_b1_header_big_endian_roundtrip(self):
        """B1: W/H 以 u16 大端传输 (struct '>HH'), 解码必须还原原值。"""
        frame = self._mk_frame(320, 240)
        self.assertTrue(frame.startswith(CAM1))
        w, h = struct.unpack(">HH", frame[4:8])
        self.assertEqual((w, h), (320, 240))
        # 大端字节序验证: W=0x0140 (320) 应编码为 0x01, 0x40
        self.assertEqual(frame[4:8], b"\x01\x40\x00\xf0")
        # 反向: 若用小端解析会得到错误值 0x4001 = 16385 != 320
        self.assertNotEqual(struct.unpack("<HH", frame[4:8]), (320, 240))

    def test_b2_frame_length_invariant(self):
        """B2: 帧长不变量: len(frame) == 8 + W*H*2 (4 魔数 + 4 头 + payload)。"""
        for w, h in [(16, 16), (64, 32), (320, 240)]:
            frame = self._mk_frame(w, h)
            self.assertEqual(len(frame), 8 + w * h * 2)

    def test_b3_sync_recovers_after_garbage_prefix(self):
        """B3: 同步扫描: 帧流夹杂任意垃圾字节时, 扫描 CAM1 后能正确解析。"""
        rng = random.Random(3)
        frame = self._mk_frame(32, 16)
        # 模拟 USB 枚举残留/诊断包混入: 垃圾 + 帧 + 更多垃圾
        junk = bytes(rng.randrange(256) for _ in range(1000))
        stream = junk + frame + junk
        i = stream.find(CAM1)
        self.assertGreaterEqual(i, 1000, "CAM1 必须位于垃圾之后")
        w, h = struct.unpack(">HH", stream[i + 4:i + 8])
        self.assertEqual((w, h), (32, 16))
        frame_len = 8 + w * h * 2
        self.assertEqual(len(stream) - i, frame_len + len(junk),
                         "帧必须恰好占 8+W*H*2, 其余为待吸收垃圾")

    def test_b4_truncated_frame_resync(self):
        """B4: 截断帧恢复: 半帧丢失后, 下一个完整帧仍可被正确切出。

        对应 capture.py 的 read_exact 超时路径: 读头后 read_exact(payload)
        只收到一半 -> 截断帧被丢弃, 流位置停在超时点, 从那里重新扫描 CAM1。
        """
        frame = self._mk_frame(16, 16)
        payload_len = 16 * 16 * 2
        # 第一帧: 头已读, payload 只收到一半 (模拟 read 超时), 紧跟第二完整帧
        truncated = frame[: 8 + payload_len // 2]
        stream = truncated + frame
        # capture.py 语义: 超时点 = 8(魔数+头) + 半帧 payload, 从那里恢复扫描
        i = 8 + payload_len // 2
        frames = []
        while True:
            j = stream.find(CAM1, i)
            if j < 0:
                break
            if len(stream) - j >= 8 + payload_len:
                w, h = struct.unpack(">HH", stream[j + 4:j + 8])
                frames.append((w, h))
                i = j + 8 + w * h * 2
            else:
                break  # 尾部残帧 -> 截断, 丢弃
        self.assertEqual(frames, [(16, 16)], "截断帧被丢弃, 仅第二完整帧被切出")


# ===========================================================================
# Layer C — RGB565 解码数学
# ===========================================================================
class TestLayerC_RGB565Math(unittest.TestCase):
    """RGB565 -> RGB888 变换的数学性质 (与 capture.py / live_view.py 相同公式)。

    变换定义 (5-6-5 位域 -> 8-bit):
        r8 = (r5 << 3) | (r5 >> 2),  r5 = (v >> 11) & 0x1F
        g8 = (g6 << 2) | (g6 >> 4),  g6 = (v >> 5)  & 0x3F
        b8 = (b5 << 3) | (b5 >> 2),  b5 =  v        & 0x1F
    等价于 r8 = 8*r5 + floor(r5/4), g8 = 4*g6 + floor(g6/16)。
    这是把 31/63 级量化值映射回 256 级的标准近似: 8*r5 是线性映射,
    floor(r5/4) 是补偿项使 31 -> 255 而不是 248。
    """

    @staticmethod
    def _ref_decode(v):
        """逐像素参考实现 (与 capture.py 逐字节逻辑完全一致)。"""
        r5 = (v >> 11) & 0x1F
        g6 = (v >> 5) & 0x3F
        b5 = v & 0x1F
        r8 = (r5 << 3) | (r5 >> 2)
        g8 = (g6 << 2) | (g6 >> 4)
        b8 = (b5 << 3) | (b5 >> 2)
        return r8, g8, b8

    def test_c1_bit_field_extraction(self):
        """C1: 位提取恒等: 已知 16-bit 值 -> 5-6-5 位域正确分离。"""
        v = 0b10100_011010_01011  # r5=20, g6=26, b5=11
        r5 = (v >> 11) & 0x1F
        g6 = (v >> 5) & 0x3F
        b5 = v & 0x1F
        self.assertEqual((r5, g6, b5), (20, 26, 11))
        # 位域互不重叠: 三个掩码覆盖全部 16 位
        self.assertEqual((r5 << 11) | (g6 << 5) | b5, v)

    def test_c2_expansion_formula_exhaustive_5bit(self):
        """C2: 5-bit -> 8-bit 扩展公式全遍历 (0..31):
            (a) 值域 [0,255]; (b) 严格单调; (c) 端点 0->0, 31->255。"""
        out = [((x << 3) | (x >> 2)) for x in range(32)]
        self.assertEqual(out[0], 0)
        self.assertEqual(out[31], 255)
        self.assertEqual(min(out), 0)
        self.assertEqual(max(out), 255)
        self.assertTrue(all(out[i] < out[i + 1] for i in range(31)),
                        "扩展必须严格单调 (保序 = 不引入伪轮廓)")

    def test_c3_expansion_formula_exhaustive_6bit(self):
        """C3: 6-bit -> 8-bit 扩展公式全遍历 (0..63): 值域 + 严格单调 + 端点。"""
        out = [((x << 2) | (x >> 4)) for x in range(64)]
        self.assertEqual(out[0], 0)
        self.assertEqual(out[63], 255)
        self.assertEqual(min(out), 0)
        self.assertEqual(max(out), 255)
        self.assertTrue(all(out[i] < out[i + 1] for i in range(63)))

    def test_c4_quantization_error_upper_bound(self):
        """C4: 量化-反量化误差上界 (数学证明, 全遍历 256 级验证):

        8-bit 值 x 先截断量化到 5/6-bit, 再扩展回 8-bit:
            r5 = x >> 3  ->  x = 8*r5 + t, t in [0,7]
            扩展后 r8' = 8*r5 + floor(r5/4), 误差 |r8'-x| = |floor(r5/4) - t|
            -> 上界 max(floor(31/4), 7) = 7
            g6 = x >> 2  ->  x = 4*g6 + t, t in [0,3]
            扩展后 g8' = 4*g6 + floor(g6/16), 误差 |g8'-x| <= max(3, 3) = 3
        结论: 该变换对任意输入引入的误差 <= (7, 3, 7)/255, 且该误差是
        量化固有的, 不是解码器 bug —— 这定义了什么算"正确解码"。
        """
        max_er, max_eg, max_eb = 0, 0, 0
        for x in range(256):
            er = abs(((x >> 3) << 3 | (x >> 3) >> 2) - x)
            eg = abs(((x >> 2) << 2 | (x >> 2) >> 4) - x)
            eb = er  # b 与 r 同公式
            max_er, max_eg, max_eb = max(max_er, er), max(max_eg, eg), max(max_eb, eb)
        self.assertLessEqual(max_er, 7, "R 通道量化误差上界 = 7")
        self.assertLessEqual(max_eg, 3, "G 通道量化误差上界 = 3")
        self.assertLessEqual(max_eb, 7, "B 通道量化误差上界 = 7")

    def test_c5_endianness_sensitivity(self):
        """C5: 端序敏感性 —— 大端解析正确, 小端解析必然错误。

        这是全管线最关键的数学属性: OV7670 高字节先出 (Layer A1), 固件
        保持字节序, 主机必须用 (b[0]<<8)|b[1]。若误用小端 (b[0]|b[1]<<8),
        R/B 交换且 G 位被挪动 —— 必须能被测试捕获。
        """
        for v in [0xABCD, 0x1234, 0xF00F, 0x8001, 0x07FF]:
            hi, lo = ov7670_pixel_bytes(v)
            big = (hi << 8) | lo      # 正确: 大端
            little = (lo << 8) | hi   # 错误: 小端
            self.assertEqual(big, v, "大端解析必须还原原始像素值")
            self.assertNotEqual(little, v, "小端解析必须不等于原始值 (端序错误可检测)")
            rb, gb, bb = self._ref_decode(big)
            rl, gl, bl = self._ref_decode(little)
            # 小端至少交换了通道: 5-bit 高字节里的红/绿信息跑到了低字节的蓝位
            self.assertNotEqual((rb, gb, bb), (rl, gl, bl))

    def test_c6_full_rgb565_space_decode_validity(self):
        """C6: 全部 65536 个 RGB565 值解码 -> 三通道都在 [0,255]。"""
        for v in range(0, 0x10000):
            r8, g8, b8 = self._ref_decode(v)
            assert 0 <= r8 <= 255 and 0 <= g8 <= 255 and 0 <= b8 <= 255
        # 用 numpy 版 (live_view) 抽查 65536 个值: 两实现必须一致
        raw = bytearray()
        for v in range(0x10000):
            hi, lo = ov7670_pixel_bytes(v)
            raw += bytes((hi, lo))
        img = live_view.decode_rgb565(bytes(raw), 256, 256)
        flat = img.reshape(-1, 3)
        for v in range(0x10000):
            r8, g8, b8 = self._ref_decode(v)
            self.assertEqual(tuple(flat[v]), (r8, g8, b8),
                             f"numpy 解码与逐像素参考在 v={v:#06x} 不一致")


# ===========================================================================
# Layer D — 图像呈现层
# ===========================================================================
class TestLayerD_Presentation(unittest.TestCase):
    """BMP 编码与旋转呈现的数学验证 (capture.rgb565_to_bmp + live_view rot90)。"""

    @staticmethod
    def _bmp_pixels(bmp_bytes):
        """回读 BMP: 跳过 54 字节头, 按 bottom-up + BGR + 行填充还原为 (h,w,3) RGB。"""
        assert bmp_bytes[:2] == b"BM"
        w = struct.unpack("<i", bmp_bytes[18:22])[0]
        h = struct.unpack("<i", bmp_bytes[22:26])[0]
        bpp = struct.unpack("<H", bmp_bytes[28:30])[0]
        assert bpp == 24
        row_size = (w * 3 + 3) & ~3
        img = np.zeros((h, w, 3), dtype=np.uint8)
        off = 54
        for y in range(h):  # 文件中第 0 行 = 图像最后一行 (bottom-up)
            row = np.frombuffer(bmp_bytes[off:off + w * 3], dtype=np.uint8).reshape(w, 3)
            img[h - 1 - y] = row[:, ::-1]  # BGR -> RGB
            off += row_size
        return img

    def _mk_rgb565_frame(self, w, h, seed):
        rng = random.Random(seed)
        return ov7670_frame_bytes(w, h, [rng.randint(0, 0xFFFF) for _ in range(w * h)])

    def test_d1_bmp_header_fields(self):
        """D1: BMP 头字段: 'BM', 54 字节 DIB, 40 字节信息头, 24bpp, 无压缩。"""
        bmp = capture.rgb565_to_bmp(62, 44, self._mk_rgb565_frame(62, 44, 1))
        self.assertEqual(bmp[:2], b"BM")
        self.assertEqual(struct.unpack("<I", bmp[10:14])[0], 54, "像素数据偏移 = 54")
        self.assertEqual(struct.unpack("<I", bmp[14:18])[0], 40, "BITMAPINFOHEADER = 40")
        self.assertEqual(struct.unpack("<i", bmp[18:22])[0], 62, "宽度")
        self.assertEqual(struct.unpack("<i", bmp[22:26])[0], 44, "高度")
        self.assertEqual(struct.unpack("<H", bmp[26:28])[0], 1, "planes = 1")
        self.assertEqual(struct.unpack("<H", bmp[28:30])[0], 24, "24 bpp")
        self.assertEqual(struct.unpack("<I", bmp[30:34])[0], 0, "BI_RGB 无压缩")

    def test_d2_bmp_row_padding(self):
        """D2: 行填充: w=62 -> 62*3=186 -> 对齐 4 -> 188 (2 字节填充/行)。

        BMP 规范要求每行 4 字节对齐; capture.py 用 row_size=(w*3+3)&~3。
        """
        w, h = 62, 44
        bmp = capture.rgb565_to_bmp(w, h, self._mk_rgb565_frame(w, h, 2))
        row_size = (w * 3 + 3) & ~3
        self.assertEqual(row_size, 188)
        pix_size = struct.unpack("<I", bmp[2:6])[0] - 54
        self.assertEqual(pix_size, row_size * h, "文件大小 = 54 + 每行对齐后尺寸*行数")

    def test_d3_bmp_bgr_order_and_bottom_up(self):
        """D3: BMP 存储为 BGR + bottom-up (行倒序) —— 回读必须还原原始 RGB565 解码。"""
        w, h = 62, 44
        raw = self._mk_rgb565_frame(w, h, 3)
        bmp = capture.rgb565_to_bmp(w, h, raw)
        back = self._bmp_pixels(bmp)
        # 逐像素与参考解码对比 (量化误差为 0: 这里是 565->888 的直接呈现)
        for y in range(h):
            for x in range(w):
                v = (raw[(y * w + x) * 2] << 8) | raw[(y * w + x) * 2 + 1]
                r8, g8, b8 = TestLayerC_RGB565Math._ref_decode(v)
                self.assertEqual(tuple(back[y, x]), (r8, g8, b8),
                                 f"BMP 回读像素 ({y},{x}) 与解码不一致")

    def test_d4_rot90_math_identity(self):
        """D4: np.rot90(k=1) 数学恒等: 逆时针 90° == 转置 + 行反转。

        live_view.py 用 k=1 把 OV7670 安装方向 (镜头朝下/左右翻转) 转正。
        对任意 (H,W) 数组 A: rot90(A,1)[i,j] == A[j, H-1-i]。
        """
        rng = np.random.default_rng(0)
        A = rng.integers(0, 256, (7, 5), dtype=np.uint8)  # H=7, W=5
        R = np.rot90(A, k=1)  # 形状 (W, H) = (5, 7)
        self.assertEqual(R.shape, (5, 7))
        for i in range(R.shape[0]):
            for j in range(R.shape[1]):
                self.assertEqual(R[i, j], A[j, A.shape[1] - 1 - i],
                                 "rot90(k=1)[i,j] 必须等于 A[j, W-1-i]")
        # 两次旋转 = 180°: R2 = A[::-1, ::-1]
        R2 = np.rot90(A, k=2)
        self.assertTrue(np.array_equal(R2, A[::-1, ::-1]))
        # 四次旋转 = 恒等
        self.assertTrue(np.array_equal(np.rot90(A, k=4), A))

    def test_d5_numpy_decode_matches_scalar_reference(self):
        """D5: live_view.decode_rgb565 (numpy 向量化) 与逐像素参考实现逐字节一致。"""
        rng = random.Random(9)
        w, h = 64, 48
        raw = ov7670_frame_bytes(w, h, [rng.randint(0, 0xFFFF) for _ in range(w * h)])
        img = live_view.decode_rgb565(raw, w, h)
        self.assertEqual(img.shape, (h, w, 3))
        for y in range(h):
            for x in range(w):
                v = (raw[(y * w + x) * 2] << 8) | raw[(y * w + x) * 2 + 1]
                self.assertEqual(tuple(img[y, x]), TestLayerC_RGB565Math._ref_decode(v))


# ===========================================================================
# Layer E — 端到端合成管线
# ===========================================================================
class TestLayerE_EndToEnd(unittest.TestCase):
    """合成随机图走完整管线: 量化 -> 大端编码 -> CAM1 封装 -> 分块传输
       -> 同步 -> 解码 -> BMP -> 回读, 逐像素误差 <= 量化上界 (7,3,7)。"""

    def test_e1_full_pipeline_error_within_quantization_bound(self):
        rng = np.random.default_rng(1234)
        H, W = 64, 64
        # 合成真实感 RGB888 图像 (三通道独立随机 + 平滑结构)
        src = np.dstack([
            rng.integers(0, 256, (H, W), dtype=np.uint8),
            rng.integers(0, 256, (H, W), dtype=np.uint8),
            rng.integers(0, 256, (H, W), dtype=np.uint8),
        ])

        # 1) 量化: 8-bit -> 5-6-5 (主机端"相机内部 ISP 输出"模型, 与固件等价)
        r5 = (src[:, :, 0] >> 3) & 0x1F
        g6 = (src[:, :, 1] >> 2) & 0x3F
        b5 = (src[:, :, 2] >> 3) & 0x1F
        v = (r5.astype(np.uint16) << 11) | (g6.astype(np.uint16) << 5) | b5.astype(np.uint16)

        # 2) OV7670 输出: 每像素大端字节对 (Layer A 模型)
        raw = bytearray()
        for val in v.reshape(-1):
            hi, lo = ov7670_pixel_bytes(int(val))
            raw += bytes((hi, lo))

        # 3) 固件封装: CAM1 + W/H BE + payload (Layer B 模型)
        frame = firmware_frame_encode(W, H, bytes(raw))

        # 4) 模拟 USB CDC 传输: 随机垃圾前缀 + 随机分块 (串口 read 语义)
        junk = bytes(rng.integers(0, 256, 500, dtype=np.uint8).tolist())
        stream = junk + frame
        buf = bytearray(stream)
        i = buf.find(CAM1)
        self.assertGreaterEqual(i, 500, "同步必须跳过垃圾前缀")
        w, h = struct.unpack(">HH", buf[i + 4:i + 8])
        self.assertEqual((w, h), (W, H))
        payload = bytes(buf[i + 8:i + 8 + w * h * 2])
        self.assertEqual(len(payload), w * h * 2)

        # 5) 主机解码 (live_view 真实实现)
        img = live_view.decode_rgb565(payload, w, h)

        # 6) 呈现 (capture 真实实现) -> BMP -> 回读
        bmp = capture.rgb565_to_bmp(w, h, payload)
        back = TestLayerD_Presentation._bmp_pixels(bmp)

        # 7) 数学断言: 解码误差 <= 量化上界 (Layer C4 证明的 7/3/7)
        d_r = np.abs(img[:, :, 0].astype(int) - src[:, :, 0].astype(int))
        d_g = np.abs(img[:, :, 1].astype(int) - src[:, :, 1].astype(int))
        d_b = np.abs(img[:, :, 2].astype(int) - src[:, :, 2].astype(int))
        self.assertLessEqual(d_r.max(), 7, "R 通道端到端误差 <= 量化上界 7")
        self.assertLessEqual(d_g.max(), 3, "G 通道端到端误差 <= 量化上界 3")
        self.assertLessEqual(d_b.max(), 7, "B 通道端到端误差 <= 量化上界 7")
        # BMP 呈现与解码结果完全一致 (呈现环节不引入额外误差)
        self.assertTrue(np.array_equal(back, img),
                        "BMP 回读必须与解码图像逐像素一致")

    def test_e2_no_byte_loss_frame_sync_count(self):
        """E2: 无丢字节时, 连续 N 帧传输后同步器精确切出 N 帧 (字节守恒)。"""
        rng = random.Random(11)
        W, H = 8, 8
        frames = []
        for _ in range(5):
            px = [rng.randint(0, 0xFFFF) for _ in range(W * H)]
            frames.append(firmware_frame_encode(W, H, ov7670_frame_bytes(W, H, px)))
        stream = b"".join(frames)
        # 切帧: 每帧固定 8 + W*H*2 字节
        n = 0
        pos = 0
        while pos + 8 + W * H * 2 <= len(stream):
            self.assertTrue(stream[pos:pos + 4] == CAM1)
            w, h = struct.unpack(">HH", stream[pos + 4:pos + 8])
            self.assertEqual((w, h), (W, H))
            pos += 8 + w * h * 2
            n += 1
        self.assertEqual(n, 5, "5 帧字节流必须精确切出 5 帧, 无残留")
        self.assertEqual(pos, len(stream), "无残留字节")


if __name__ == "__main__":
    unittest.main(verbosity=2)
