#!/usr/bin/env python3
"""OV7670 raw Bayer 硬件集成测试 (test_hw_bayer.py)

在真实 YD-RP2040 + OV7670 上验证 RAW_BAYER 固件 (CAM2 帧协议) 的可观测面:

  1. 'R' 寄存器回读 (DBG1+0xFB): COM7=0x01 (sensor raw), COM15=0xC0 (shipped)
     或 0xD0 (official), PID=0x76, 窗口/缩放寄存器 —— 且在任何 'T' 之前断言 (test_01)
  2. CAM2 帧流: 'T' upper 切换 ack (DBG1+0xF9+0x00), 帧头 W/H=320×240,
     载荷 76800 B, 统计特征 = 真实图像 (非全零/伪数据)
  3. 缝合 + 去马赛克 + BMP: stitch_halves -> (480,320), demosaic -> RGB,
     rgb_to_bmp 头/尺寸公式一致, 落盘字节与内存一致
  4. PCLK 计数: 'C' 命令 -> ~640 PCLK/line (official config at 320×240)

固件版本前提: 板上必须烧 RAW_BAYER 构建。setUpClass 用 'R' 探针读 COM7:
COM7 != 0x01 -> 整类 SkipTest (非 raw bayer 固件)。test_hw_integration.py
的探针相反 —— 两文件互补: 真机上跑哪套固件, 就只跑对应的一套断言。

Default build (env:rpipico) = official Table 2-2 registers + per-PCLK sampling
(DCWCTR=0x11 HDS×2 → 320 distinct bytes/line, matches FRAME_W=320).
Legacy build (env:rpipico_legacy) = shipped寄存器 + every-2nd-PCLK sampling
(designed for 640×480; at 320×240 outputs 640 distinct bytes/line but DMA
captures only 320). CLKRC readback auto-detects which build is running.

串口读法 (重要约束): 同 test_hw_integration.py —— 大块 read(4096) + 内部
缓冲 + find() 滑窗同步。主机吞吐必须高于帧流, 否则 Serial.write 阻塞把
命令饿死 (实测过的硬约束)。本文件绝不发送 'B'。

无串口设备/模式不符时整类 skip, 不假装通过也不报错。
"""

import glob
import struct
import sys
import tempfile
import time
import unittest

import numpy as np

try:
    import serial
except ImportError:
    serial = None  # 无 pyserial: 硬件用例整体 skip

sys.path.insert(0, __file__.rsplit("/", 2)[0])  # 仓库根 (bayer_capture/bayer_demosaic)

import bayer_capture as bc   # noqa: E402
import bayer_demosaic as bd  # noqa: E402

CAM2 = b"CAM2"
DBG1 = b"DBG1"
REG_MARKER = 0xFB
WINDOW_MARKER = 0xF9
PCLK_MARKER = 0xF8

EXPECT_W, EXPECT_H = 320, 240
EXPECT_PAYLOAD = EXPECT_W * EXPECT_H  # 76800, 1 byte/px

# 'R' 回读的关键寄存器 -> 期望值 (RAW_BAYER 构建, src/main.cpp 24-reg 表;
# COM10/COM8 为 live 状态不固定断言; REG74=0x20 1x 水平缩放比 (Table 6-1);
# REG75 未写入, 断言 reset 默认值)。双表: shipped (默认) 与 official
# (RAW_BAYER_OFFICIAL_REGS 构建, 官方 Table 2-2 Sheet 3 值; 差异寄存器为
# CLKRC/COM14/XSC/YSC/DCWCTR/PCLK_DIV)。setUpClass 按 CLKRC 探针自动选择。
EXPECTED_REGS_SHIPPED = {
    0x0A: 0x76,  # PID          OV7670
    0x0B: 0x73,  # VER
    0x12: 0x01,  # COM7         sensor raw 8-bit Bayer out
    0x40: 0xC0,  # COM15        full 0-255 range (no RGB565 — fixes D7=D0)
    0x11: 0x80,  # CLKRC 20.8 MHz build (fINT=XCLK/2; Table 2-2 0x01 targets 24 MHz)
    0x6B: 0x0A,  # DBLV         PLL
    0x1E: 0x07,  # MVFP         无翻转
    0x17: 0x11,  # HSTART       全窗
    0x18: 0x61,  # HSTOP
    0x19: 0x03,  # VSTART       全窗 (init 后、任何 'T' 之前)
    0x1A: 0x7B,  # VSTOP
    0x03: 0x03,  # VREF
    0x32: 0x80,  # HREF
    0x70: 0x00,  # SCALING_XSC: scaler bypass (live-verified; 0x3A 非 dup 因)
    0x71: 0x00,  # SCALING_YSC
    0x0C: 0x00,  # COM3         zoom/downsampling bypass
    0x3E: 0x18,  # COM14        bit4+bit3 open the 0x73 gate; bits[2:0]=000 PCLK /1
    0x72: 0x00,  # DCWCTR      NO down sampling (HDS=00; 0x11 HDS by 2 -> dup)
    0x73: 0x08,  # PCLK_DIV    bit[3]=1 bypass divider (matches RGB565; 0xF0 enable -> /2 dup)
    0x74: 0x20,  # REG74       Horizontal Scaling Ratio 0x20/REG74[6:0]; 0x20=1x (Table 6-1); 0x00 undefined -> dup
    0x75: 0x0F,  # REG75       init 未写入; reset 默认
    0xA2: 0x02,  # PCLK_DELAY  Table 2-2 VGA raw ref
}
EXPECTED_REGS_OFFICIAL = {
    0x0A: 0x76,  # PID          OV7670
    0x0B: 0x73,  # VER
    0x12: 0x01,  # COM7         sensor raw 8-bit Bayer out
    0x40: 0xD0,  # COM15        full 0-255 range (official table keeps RGB565 bit)
    0x11: 0x01,  # CLKRC        official Table 2-2 Sheet 3 (24 MHz input ref)
    0x6B: 0x0A,  # DBLV         PLL
    0x1E: 0x07,  # MVFP         无翻转
    0x17: 0x11,  # HSTART       全窗
    0x18: 0x61,  # HSTOP
    0x19: 0x03,  # VSTART       全窗 (init 后、任何 'T' 之前)
    0x1A: 0x7B,  # VSTOP
    0x03: 0x03,  # VREF
    0x32: 0x80,  # HREF
    0x70: 0x3A,  # SCALING_XSC  official Table 2-2 Sheet 3
    0x71: 0x35,  # SCALING_YSC
    0x0C: 0x00,  # COM3         official (= shipped)
    0x3E: 0x00,  # COM14        official
    0x72: 0x11,  # DCWCTR      official (HDS by 2 per Table 6-2)
    0x73: 0xF0,  # PCLK_DIV    official (enable divider)
    0x74: 0x20,  # REG74       kept 1x horizontal ratio (not in Table 2-2)
    0x75: 0x0F,  # REG75       init 未写入; reset 默认
    0xA2: 0x02,  # PCLK_DELAY  official (= shipped)
}


def find_port():
    """自动探测 /dev/cu.usbmodem* (macOS) 或 COM* (Windows)。"""
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("COM*"))
    return ports[0] if ports else None


class SyncError(TimeoutError):
    pass


class SerialStream:
    """大块读 + 内部缓冲 (同 test_hw_integration.py 的硬约束)。"""

    CHUNK = 4096

    def __init__(self, s):
        self.s = s
        self.buf = b""

    def _fill(self, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            chunk = self.s.read(self.CHUNK)
            if chunk:
                self.buf += chunk
                return True
        return False

    def read_exact(self, n, timeout=5):
        t0 = time.time()
        while len(self.buf) < n and time.time() - t0 < timeout:
            self._fill(max(0.05, timeout - (time.time() - t0)))
        if len(self.buf) < n:
            raise SyncError(f"read_exact({n}) 超时, 仅收到 {len(self.buf)} B")
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def sync(self, magic, timeout=5):
        t0 = time.time()
        keep = len(magic) - 1
        while time.time() - t0 < timeout:
            idx = self.buf.find(magic)
            if idx >= 0:
                self.buf = self.buf[idx + len(magic):]
                return
            self.buf = self.buf[-keep:]  # 防魔数跨块截断
            self._fill(max(0.05, timeout - (time.time() - t0)))
        raise SyncError(f"同步 {magic} 超时 ({timeout}s)")

    def drain(self):
        self.buf = b""
        self.s.reset_input_buffer()


@unittest.skipIf(serial is None, "pyserial 未安装, 跳过硬件集成测试")
class TestRawBayerHardware(unittest.TestCase):
    """真机用例 (RAW_BAYER 固件): 寄存器 / CAM2 帧 / CFA 分离 / 缝合+去马赛克。"""

    port = find_port()

    @classmethod
    def setUpClass(cls):
        if not cls.port:
            raise unittest.SkipTest(f"无串口设备 ({cls.port}), 跳过硬件测试")
        cls.s = serial.Serial(cls.port, 115200, timeout=2)
        cls.st = SerialStream(cls.s)
        cls.st.drain()
        # 固件模式探针: COM7 != 0x01 -> 非 raw bayer 固件, 整类 skip。
        try:
            cls.s.write(b"R")
            cls.s.flush()
            cls.st.sync(DBG1, timeout=6)
            m = cls.st.read_exact(1, timeout=2)
            if m[0] != REG_MARKER:
                raise unittest.SkipTest("'R' 探针未收到 DBG1+0xFB 包")
            n = cls.st.read_exact(1, timeout=2)
            body = cls.st.read_exact(n[0] * 2, timeout=5)
            regs = dict(body[i:i + 2] for i in range(0, len(body), 2))
            if regs.get(0x12) != 0x01:
                raise unittest.SkipTest(
                    f"板上非 raw bayer 固件 (COM7=0x{regs.get(0x12, -1):02X}), "
                    "CAM2 用例跳过 (test_hw_integration 负责 RGB565 固件)")
            # 第 8 组 A/B: CLKRC=0x01 -> official Table 2-2 构建 (RAW_BAYER_
            # OFFICIAL_REGS), 否则 shipped 构建。test_01 断言对应期望表。
            cls.EXPECTED_REGS = (EXPECTED_REGS_OFFICIAL if regs.get(0x11) == 0x01
                                 else EXPECTED_REGS_SHIPPED)
        except SyncError as e:
            raise unittest.SkipTest(f"固件模式探针失败: {e}")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "s"):
            cls.s.close()

    def _sync(self, magic, timeout=5):
        try:
            self.st.sync(magic, timeout)
        except SyncError as e:
            self.fail(str(e))

    def _read_exact(self, n, timeout=5):
        try:
            return self.st.read_exact(n, timeout)
        except SyncError as e:
            self.fail(str(e))

    def _read_dbg_packet(self, marker, timeout=5):
        self._sync(DBG1, timeout)
        m = self._read_exact(1, timeout=2)
        if m[0] != marker:
            self.fail(f"DBG1 后 marker 应为 0x{marker:02x}, 得到 0x{m[0]:02x}")
        return m

    def _switch_window(self, half):
        """'T'+参数字节 -> 断言 ack DBG1+0xF9+param, 等 ~0.5s 窗口稳定。"""
        param = bc.encode_bayer_window(half)
        self.s.write(b"T" + param)
        self.s.flush()
        self._read_dbg_packet(WINDOW_MARKER, timeout=6)
        ack = self._read_exact(1, timeout=2)
        self.assertEqual(ack, param,
                         f"'T' {half} ack 应回显 {param!r}, 实际 {ack!r} (0xFF=SCCB失败)")
        time.sleep(0.5)  # ~2 帧窗口稳定

    def _capture_cam2(self, timeout=15):
        """同步 CAM2 魔数, 读帧头 + 完整 76800 B 载荷 -> (H, W) uint8。"""
        self._sync(CAM2, timeout=8)
        hdr = self._read_exact(4, timeout=3)
        w, h = struct.unpack(">HH", hdr)
        self.assertEqual((w, h), (EXPECT_W, EXPECT_H),
                         f"CAM2 帧尺寸应为 {EXPECT_W}x{EXPECT_H}")
        raw = self._read_exact(EXPECT_PAYLOAD, timeout=timeout)
        self.assertEqual(len(raw), EXPECT_PAYLOAD, "载荷必须完整 76800 B")
        return np.frombuffer(raw, dtype=np.uint8).reshape(h, w)

    # ---- 用例 (test_01 在前: 先于任何 'T' 断言全窗寄存器) ----

    def test_01_reg_readback_raw_bayer_mode(self):
        """'R': 24 寄存器回读, 关键位证明 raw bayer 模式 (COM7=0x01)。"""
        self.s.write(b"R")
        self.s.flush()
        self._read_dbg_packet(REG_MARKER, timeout=6)
        n = self._read_exact(1, timeout=2)
        self.assertEqual(n, b"\x18", "寄存器数量应为 24")
        body = self._read_exact(24 * 2, timeout=5)
        self.assertEqual(len(body), 48, "24 × (reg,val) 完整")
        regs = dict(body[i:i + 2] for i in range(0, len(body), 2))
        # 跨次运行残留: 上次 test_03/04 把窗口切到 half, init 全窗值不再成立
        vstart, vstop = regs.get(0x19, -1), regs.get(0x1A, -1)
        if vstart == 0x3F or vstop == 0x3F:
            raise unittest.SkipTest(
                f"板上窗口残留 half 态 (VSTART=0x{vstart:02X}, VSTOP=0x{vstop:02X}); "
                "test_01 断言 init 全窗值, 请重新上电/重烧固件后运行")
        for reg, expected in self.__class__.EXPECTED_REGS.items():
            self.assertEqual(regs.get(reg), expected,
                             f"寄存器 0x{reg:02X} 应为 0x{expected:02X} (raw bayer), "
                             f"实际 0x{regs.get(reg, -1):02X}")

    def test_02_cam2_frame_stats(self):
        """'T' upper -> ack; CAM2 帧头 320x240, 载荷 76800 B, 统计=真实图像。"""
        self._switch_window("upper")
        cfa = self._capture_cam2()
        raw = cfa.tobytes()
        nz = sum(1 for x in raw if x != 0)
        distinct = len(set(raw))
        self.assertGreater(nz, EXPECT_PAYLOAD // 2,
                           f"真实帧应有 >50% 非零字节, 实际 {nz}/{EXPECT_PAYLOAD}")
        self.assertGreater(distinct, 100,
                           f"真实帧应含 >100 种字节值, 实际 {distinct}")

    def test_03_no_horizontal_duplication(self):
        """上/下半帧无水平 2x 重复 (dup_even < 0.9): 每字节必须独立采样。

        Default build uses official Table 2-2 registers + per-PCLK sampling.
        DCWCTR=0x11 applies HDS×2 downsampling, producing 320 distinct bytes/line
        that match FRAME_W=320. Per-PCLK sampling captures each once — no
        duplication expected. Legacy shipped build (every-2nd-PCLK) was the
        workaround for the 640×480 dup issue (T7, 2026-08-14).
        """
        self._switch_window("upper")
        upper = self._capture_cam2()
        self._switch_window("lower")
        lower = self._capture_cam2()
        self.__class__.pair = (upper, lower)  # 供 test_04 复用, 免重复采集
        for label, half in (("upper", upper), ("lower", lower)):
            dup_even = float((half[:, 0::2] == half[:, 1::2]).mean())
            self.assertLess(dup_even, 0.90,
                            f"{label} 半帧偶数/奇数列逐位相等率 "
                            f"{dup_even:.3f} >= 0.9: 疑似 2 PCLK/byte 字节重复")

    def test_04_stitch_demosaic_bmp(self):
        """缝合 (480,320) + 去马赛克 RGB + BMP 头/尺寸/落盘一致。"""
        pair = getattr(self.__class__, "pair", None)
        if pair is None:
            self._switch_window("upper")
            upper = self._capture_cam2()
            self._switch_window("lower")
            lower = self._capture_cam2()
        else:
            upper, lower = pair
        full = bc.stitch_halves(upper, lower)
        self.assertEqual(full.shape, (480, 320))
        rgb = bd.demosaic_bayer(full)
        self.assertEqual(rgb.shape, (480, 320, 3))
        self.assertEqual(rgb.dtype, np.uint8)
        bmp = bd.rgb_to_bmp(320, 480, rgb)
        self.assertEqual(bmp[:2], b"BM")
        row_size = (320 * 3 + 3) & ~3
        self.assertEqual(len(bmp), 54 + row_size * 480,
                         "BMP 长度 = 54 + 行填充对齐后 480 行")
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/frame_320x480.bmp"
            with open(path, "wb") as f:
                f.write(bmp)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), bmp, "落盘 BMP 与内存字节一致")

    def test_05_pclk_count_per_line(self):
        """'C': SM2 直接计数每行 PCLK 上升沿 -> ~640 (official config at 320×240)。

        Default build now uses official Table 2-2 registers (DCWCTR=0x11 HDS×2,
        PCLK_DIV=0xF0) + per-PCLK sampling. The sensor outputs 640 PCLKs/line
        with 320 distinct bytes after downsampling, matching FRAME_W=320.
        Legacy shipped build (every-2nd-PCLK) produces 1280 PCLK/line.
        'C' counts edges directly via SM2 PIO counter.
        """
        self.s.write(b"C")
        self.s.flush()
        self._read_dbg_packet(PCLK_MARKER, timeout=6)
        n = self._read_exact(1, timeout=2)[0]
        self.assertGreaterEqual(n, 2,
                                f"'C' 应收到 >=2 行计数, 实际 {n} (相机无帧?)")
        body = self._read_exact(n * 4, timeout=5)
        edges = [0xFFFFFFFF - struct.unpack(">I", body[i:i + 4])[0]
                 for i in range(0, len(body), 4)]
        spread = max(edges) - min(edges)
        self.assertLessEqual(spread, 3,
                             f"各行 PCLK 数应一致 (±3 边沿同步竞态), 实际 {edges}")
        e = edges[0]
        self.assertTrue(abs(e - 640) <= 3 or abs(e - 1280) <= 3,
                        f"每行 PCLK 上升沿应为 ~640 或 ~1280, 实际 {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
