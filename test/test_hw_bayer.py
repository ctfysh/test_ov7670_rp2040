#!/usr/bin/env python3
"""OV7670 raw Bayer 硬件集成测试 (test_hw_bayer.py)

在真实 YD-RP2040 + OV7670 上验证 RAW_BAYER 固件 (CAM2 帧协议) 的可观测面:

  1. 'R' 寄存器回读 (DBG1+0xFB): COM7=0x01 (sensor raw), COM15=0xD0,
     PID=0x76, 窗口/缩放寄存器 = 全窗值 —— 且在任何 'T' 之前断言 (test_01)
  2. CAM2 帧流: 'T' upper 切换 ack (DBG1+0xF9+0x00), 帧头 W/H=640×240,
     载荷 153600 B, 统计特征 = 真实图像 (非全零/伪数据)
  3. CFA 分离: 上/下半帧 cfa_means, 三通道均值差 >4 —— 输出确为拜耳 CFA
  4. 缝合 + 去马赛克 + BMP: stitch_halves -> (480,640), demosaic -> RGB,
     rgb_to_bmp 头/尺寸公式一致, 落盘字节与内存一致

固件版本前提: 板上必须烧 RAW_BAYER 构建。setUpClass 用 'R' 探针读 COM7:
COM7 != 0x01 -> 整类 SkipTest (非 raw bayer 固件)。test_hw_integration.py
的探针相反 —— 两文件互补: 真机上跑哪套固件, 就只跑对应的一套断言。

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

EXPECT_W, EXPECT_H = 640, 240
EXPECT_PAYLOAD = EXPECT_W * EXPECT_H  # 153600, 1 byte/px

# 'R' 回读的关键寄存器 -> 期望值 (RAW_BAYER 构建, src/main.cpp 17-reg 表;
# COM10/COM8 为 live 状态不固定断言)
EXPECTED_REGS = {
    0x0A: 0x76,  # PID          OV7670
    0x0B: 0x73,  # VER
    0x12: 0x01,  # COM7         sensor raw 8-bit Bayer out
    0x40: 0xD0,  # COM15        full 0-255 range
    0x11: 0x01,  # CLKRC        Table 2-2
    0x6B: 0x0A,  # DBLV         PLL
    0x1E: 0x07,  # MVFP         无翻转
    0x17: 0x11,  # HSTART       全窗
    0x18: 0x61,  # HSTOP
    0x19: 0x03,  # VSTART       全窗 (init 后、任何 'T' 之前)
    0x1A: 0x7B,  # VSTOP
    0x03: 0x03,  # VREF
    0x32: 0x80,  # HREF
    0x70: 0x3A,  # SCALING_XSC
    0x71: 0x35,  # SCALING_YSC
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
        """同步 CAM2 魔数, 读帧头 + 完整 153600 B 载荷 -> (H, W) uint8。"""
        self._sync(CAM2, timeout=8)
        hdr = self._read_exact(4, timeout=3)
        w, h = struct.unpack(">HH", hdr)
        self.assertEqual((w, h), (EXPECT_W, EXPECT_H),
                         f"CAM2 帧尺寸应为 {EXPECT_W}x{EXPECT_H}")
        raw = self._read_exact(EXPECT_PAYLOAD, timeout=timeout)
        self.assertEqual(len(raw), EXPECT_PAYLOAD, "载荷必须完整 153600 B")
        return np.frombuffer(raw, dtype=np.uint8).reshape(h, w)

    # ---- 用例 (test_01 在前: 先于任何 'T' 断言全窗寄存器) ----

    def test_01_reg_readback_raw_bayer_mode(self):
        """'R': 17 寄存器回读, 关键位证明 raw bayer 模式 (COM7=0x01)。"""
        self.s.write(b"R")
        self.s.flush()
        self._read_dbg_packet(REG_MARKER, timeout=6)
        n = self._read_exact(1, timeout=2)
        self.assertEqual(n, b"\x11", "寄存器数量应为 17")
        body = self._read_exact(17 * 2, timeout=5)
        self.assertEqual(len(body), 34, "17 × (reg,val) 完整")
        regs = dict(body[i:i + 2] for i in range(0, len(body), 2))
        for reg, expected in EXPECTED_REGS.items():
            self.assertEqual(regs.get(reg), expected,
                             f"寄存器 0x{reg:02X} 应为 0x{expected:02X} (raw bayer), "
                             f"实际 0x{regs.get(reg, -1):02X}")

    def test_02_cam2_frame_stats(self):
        """'T' upper -> ack; CAM2 帧头 640x240, 载荷 153600 B, 统计=真实图像。"""
        self._switch_window("upper")
        cfa = self._capture_cam2()
        raw = cfa.tobytes()
        nz = sum(1 for x in raw if x != 0)
        distinct = len(set(raw))
        self.assertGreater(nz, EXPECT_PAYLOAD // 2,
                           f"真实帧应有 >50% 非零字节, 实际 {nz}/{EXPECT_PAYLOAD}")
        self.assertGreater(distinct, 100,
                           f"真实帧应含 >100 种字节值, 实际 {distinct}")

    def test_03_cfa_separation(self):
        """上/下半帧 cfa_means: 三通道均值差 >4 -> 输出确为拜耳 CFA。"""
        self._switch_window("upper")
        upper = self._capture_cam2()
        self._switch_window("lower")
        lower = self._capture_cam2()
        self.__class__.pair = (upper, lower)  # 供 test_04 复用, 免重复采集
        for label, half in (("upper", upper), ("lower", lower)):
            means = bc.cfa_means(half)
            spread = max(means.values()) - min(means.values())
            self.assertGreater(spread, 4.0,
                               f"{label} 半帧 CFA 三通道均值差应 >4 (分离), "
                               f"实际 {spread:.2f}: {means}")

    def test_04_stitch_demosaic_bmp(self):
        """缝合 (480,640) + 去马赛克 RGB + BMP 头/尺寸/落盘一致。"""
        pair = getattr(self.__class__, "pair", None)
        if pair is None:
            self._switch_window("upper")
            upper = self._capture_cam2()
            self._switch_window("lower")
            lower = self._capture_cam2()
        else:
            upper, lower = pair
        full = bc.stitch_halves(upper, lower)
        self.assertEqual(full.shape, (480, 640))
        rgb = bd.demosaic_bayer(full)
        self.assertEqual(rgb.shape, (480, 640, 3))
        self.assertEqual(rgb.dtype, np.uint8)
        bmp = bd.rgb_to_bmp(640, 480, rgb)
        self.assertEqual(bmp[:2], b"BM")
        row_size = (640 * 3 + 3) & ~3
        self.assertEqual(len(bmp), 54 + row_size * 480,
                         "BMP 长度 = 54 + 行填充对齐后 480 行")
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/frame_640x480.bmp"
            with open(path, "wb") as f:
                f.write(bmp)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), bmp, "落盘 BMP 与内存字节一致")


if __name__ == "__main__":
    unittest.main(verbosity=2)
