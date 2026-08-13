#!/usr/bin/env python3
"""OV7670 硬件集成测试 (test_hw_integration.py)

在真实 YD-RP2040 + OV7670 板上验证 main 固件 (PIO+DMA CDC) 行为的三个
可观测面, 与 test_pipeline_math.py 的纯数学验证互补 —— 这里断言的是
"真机上的真实字节流":

  1. 'R' 寄存器回读 (DBG1+0xFB): 证明传感器处于 RGB565 模式
     COM7=0x14 (QVGA+RGB), COM15=0xD0 (RGB565 全范围), PID=0x76, VER=0x73
  2. CAM1 帧流: 帧头 W/H 正确, 载荷 153600 字节, 统计特征 = 真实图像
     (大量非零字节 + 多种取值), 而非全零/伪数据
  3. 'S' 慢速波形 (DBG1+0xFC): 400 样本 @100us, HREF 出现多次边沿
     (QVGA 每帧 ~240 行门控), 证明 PIO 采集时钟链在真机上工作

固件版本前提: 板上必须烧录 main 分支固件 (CAM1 帧协议)。若烧的是
impl/uvc (TinyUSB UVC webcam), 视频走 UVC 接口不走 CDC, CAM1 同步必
超时 —— 本测试不会假装通过。检查: macOS 系统设置/系统信息里板子若以
"UVC Camera" 出现, 即非 main 固件。

串口读法 (重要): 帧流 660KB/s 持续到达, 若主机逐字节 read(1) 慢读,
USB CDC 缓冲填满, 板子 Serial.write 阻塞, loop() 卡在发帧导致 'S'/'R'
命令饿死。因此本文件用大块 read(4096) + 内部缓冲 + find() 滑窗同步,
保证主机吞吐始终高于帧流。这是踩过坑的硬约束。

无串口设备时整类 skip (CI/无板环境), 不假装通过也不报错。

运行: python3 -m unittest discover -s test -p "test_*.py"
      (板连在 /dev/cu.usbmodem* 时自动跑硬件用例)
"""

import glob
import struct
import sys
import time
import unittest

try:
    import serial
except ImportError:
    serial = None  # 无 pyserial: 硬件用例整体 skip

sys.path.insert(0, __file__.rsplit("/", 2)[0])  # 仓库根 (复用 capture.py 若需要)

CAM1 = b"CAM1"
DBG1 = b"DBG1"
REG_MARKER = 0xFB
WAVE_MARKER = 0xFC
WAVE_TYPE_SLOW = 0x02
WAVE_SLOW_SAMPLES = 400

EXPECT_W, EXPECT_H = 320, 240
EXPECT_PAYLOAD = EXPECT_W * EXPECT_H * 2  # 153600

# 'R' 回读的关键寄存器 -> 期望值 (见 src/main.cpp reg_readback_send)
EXPECTED_REGS = {
    0x0A: 0x76,  # PID   OV7670
    0x0B: 0x73,  # VER
    0x12: 0x14,  # COM7  QVGA + RGB (bit4 QVGA, bit2 RGB) -> RGB565 输出
    0x40: 0xD0,  # COM15 RGB565 全范围输出
    0x15: 0x02,  # COM10
    0x6B: 0x0A,  # DBLV  PLL x2
    0x1E: 0x07,  # MVFP  无翻转
    0x13: 0xE7,  # COM8  AGC+AEC+AWB 开
}


def find_port():
    """自动探测 /dev/cu.usbmodem* (macOS) 或 COM* (Windows)。"""
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("COM*"))
    return ports[0] if ports else None


class SyncError(TimeoutError):
    pass


class SerialStream:
    """大块读 + 内部缓冲: 主机吞吐 > 帧流, 且 find() 滑窗不遗漏魔数。

    - read_exact(n): 从缓冲取 n 字节, 不足则继续大块读, 超时抛 SyncError
    - sync(magic): 在累积缓冲里 find 魔数 (任意位置, 跨块安全), 命中后
      丢弃魔数及之前的字节, 之后的字节留在缓冲供后续 read_exact 消费
    - drain(): 丢弃内部缓冲 + OS 缓冲 (测量帧间隔前清理预存帧头)
    """

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
            # 只保留末尾 len(magic)-1 字节, 防止魔数跨块被截断
            self.buf = self.buf[-keep:]
            self._fill(max(0.05, timeout - (time.time() - t0)))
        raise SyncError(f"同步 {magic} 超时 ({timeout}s)")

    def drain(self):
        self.buf = b""
        self.s.reset_input_buffer()


@unittest.skipIf(serial is None, "pyserial 未安装, 跳过硬件集成测试")
class TestHardwareIntegration(unittest.TestCase):
    """真机用例: 寄存器回读 / 帧流统计 / 慢速波形 / 帧率量级。"""

    port = find_port()

    @classmethod
    def setUpClass(cls):
        if not cls.port:
            raise unittest.SkipTest(f"无串口设备 ({cls.port}), 跳过硬件集成测试")
        cls.s = serial.Serial(cls.port, 115200, timeout=2)
        cls.st = SerialStream(cls.s)
        cls.st.drain()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "s"):
            cls.s.close()

    # ---- 串口原语 (超时 -> 测试失败) ----

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
        """等 DBG1 魔数 + marker 字节, 返回 marker 后的包体。"""
        self._sync(DBG1, timeout)
        m = self._read_exact(1, timeout=2)
        if m[0] != marker:
            self.fail(f"DBG1 后 marker 应为 0x{marker:02x}, 得到 0x{m[0]:02x}")
        return m

    # ---- 用例 ----

    def test_register_readback_rgb565_mode(self):
        """'R': 17 寄存器回读, 关键位证明 RGB565 模式 (COM7=0x14, COM15=0xD0)。"""
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
                             f"寄存器 0x{reg:02X} 应为 0x{expected:02X} "
                             f"(RGB565 模式), 实际 0x{regs.get(reg, -1):02X}")

    def test_cam1_frame_stream_valid_image(self):
        """CAM1: 帧头 W/H=320×240, 载荷 153600 B, 统计 = 真实图像 (非全零)。"""
        self._sync(CAM1, timeout=8)
        hdr = self._read_exact(4, timeout=3)
        self.assertEqual(len(hdr), 4, "帧头 4 字节 (W,H u16 BE)")
        w, h = struct.unpack(">HH", hdr)
        self.assertEqual((w, h), (EXPECT_W, EXPECT_H),
                         f"固件帧尺寸应为 {EXPECT_W}x{EXPECT_H}")
        raw = self._read_exact(EXPECT_PAYLOAD, timeout=15)
        self.assertEqual(len(raw), EXPECT_PAYLOAD, "载荷必须完整 153600 B")
        # 真实图像特征: 大量非零字节 + 多种取值 (全零 = IN_SHIFTDIR 类采集 bug)
        nz = sum(1 for x in raw if x != 0)
        distinct = len(set(raw))
        self.assertGreater(nz, EXPECT_PAYLOAD // 2,
                           f"真实帧应有 >50% 非零字节, 实际 {nz}/{EXPECT_PAYLOAD}")
        self.assertGreater(distinct, 100,
                           f"真实帧应含 >100 种字节值, 实际 {distinct}")

    def test_slow_waveform_href_edges(self):
        """'S': 400 样本 @100us, 40ms 窗口内 HREF 多次翻转 (行门控在跑)。"""
        self.s.write(b"S")
        self.s.flush()
        self._read_dbg_packet(WAVE_MARKER, timeout=8)
        body = self._read_exact(1 + 2 + 4, timeout=3)  # type + count(BE) + dur_us(BE)
        self.assertEqual(len(body), 7, "波形头: type(1)+count(2)+dur_us(4)")
        wtype, count, dur_us = (body[0], struct.unpack(">H", body[1:3])[0],
                                struct.unpack(">I", body[3:7])[0])
        self.assertEqual(wtype, WAVE_TYPE_SLOW, "'S' 应回慢速波形 type=0x02")
        self.assertEqual(count, WAVE_SLOW_SAMPLES, "'S' 样本数应为 400")
        self.assertGreater(dur_us, 1000, "采集窗口应 >1ms (400×100us≈40ms)")
        samples_raw = self._read_exact((count + 1) // 2, timeout=3)
        self.assertEqual(len(samples_raw), 200, "400 样本按 2 位/字节打包 = 200 B")
        # 解包: 高 nibble = 偶数样本, 低 nibble = 奇数样本; bit2 = HREF
        bits = []
        for i, byte in enumerate(samples_raw):
            bits.append(byte >> 4)
            bits.append(byte & 0x0F)
        bits = bits[:count]
        href = [1 if b & 0x04 else 0 for b in bits]
        edges = sum(1 for i in range(1, len(href)) if href[i] != href[i - 1])
        self.assertGreater(edges, 10,
                           f"40ms 窗口内 HREF 应有 >10 次翻转 (每行 2 次), 实际 {edges}")
        self.assertLess(edges, count, "HREF 不应整窗口抖动 (采样饱和)")

    def test_frame_rate_sanity(self):
        """帧率量级: 两帧间隔在 50ms~2s (CDC 瓶颈 ~4 FPS, 非采集 bug 量级)。

        测量方法: 同步并消费完整一帧后 drain() 清掉缓冲里预存的帧头
        (否则立即命中, dt≈0 测的是缓冲预存而非真实帧间隔), 再等两个
        相邻 CAM1 头计时。
        """
        self._sync(CAM1, timeout=8)
        hdr = self._read_exact(4, timeout=3)
        w, h = struct.unpack(">HH", hdr)
        self._read_exact(w * h * 2, timeout=15)  # 消费完整第一帧
        self.st.drain()  # 丢弃预存的下一帧头, 开始干净计时
        self._sync(CAM1, timeout=8)  # 等第一个头 (真实等待)
        t0 = time.time()
        self._sync(CAM1, timeout=8)  # 等第二个头
        dt = time.time() - t0
        self.assertGreater(dt, 0.05, f"帧间隔应 >50ms (物理上限), 实际 {dt:.2f}s")
        self.assertLess(dt, 2.0, f"帧间隔应 <2s (采集/发送未死锁), 实际 {dt:.2f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
