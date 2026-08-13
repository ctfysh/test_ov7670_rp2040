# 测试目录（test/）

本目录对 OV7670 采集管线做**全链路验证**，分两层互补：

| 层 | 文件 | 是否需硬件 | 验证对象 |
|----|------|-----------|---------|
| 纯软件数学验证 | `test_pipeline_math.py` | 否（CI 可跑） | 帧协议解析 → RGB565 位提取/扩展 → BMP 呈现的数学正确性 |
| 纯软件数学验证 | `test_bayer_math.py` | 否（CI 可跑） | 拜耳去马赛克数学（Layers F/G/H）：RGGB 提取/近邻插值/BMP |
| 纯软件数学验证 | `test_bayer_capture.py` | 否（CI 可跑） | CAM2 帧封装/滑窗解析/缝合/窗口编码/CFA 均值（Layers I/J/K） |
| 硬件集成验证 | `test_hw_integration.py` | 是（真机） | 真机上寄存器回读 / CAM1 帧流 / PIO 波形 / 帧率量级（RGB565 固件） |
| 硬件集成验证 | `test_hw_bayer.py` | 是（真机） | 真机上寄存器回读 / CAM2 帧流 / CFA 分离 / 缝合+去马赛克（raw bayer 固件） |

## 快速开始

```bash
# 全套（硬件用例在检测到串口设备时自动跑；无板则自动 skip）
python3 -m unittest discover -s test -p "test_*.py"

# 只跑数学层（无需硬件）
python3 -m unittest test.test_pipeline_math -v

# 只跑硬件层（板已连 /dev/cu.usbmodem*）
python3 -m unittest test.test_hw_integration -v
```

依赖：`pyserial`（仅硬件层；数学层零依赖）。作者环境：
`~/.platformio/penv/bin/python3`（PlatformIO 自带 Python）。

## test_pipeline_math.py — 数学正确性（20 用例）

把"从最原始数据到最终呈现 RGB 图像"拆成 5 层，每层用数学恒等/全遍历证明：

- **Layer A 采集字节流**（A1–A2）：帧头/载荷在字节流中的定位不变式
- **Layer B 帧协议**（B1–B4）：魔数同步、垃圾字节容错、截断帧恢复、无字节丢失时同步器精确切帧（字节守恒）
- **Layer C RGB565 数学**（C1–C6）：5-6-5 位域提取恒等、5-bit/6-bit→8-bit 扩展公式**全遍历**（0..31 / 0..63）验证值域+严格单调+端点、量化-反量化误差上界证明、端序敏感性（大端正确/小端必然错）、全部 65536 个 RGB565 值解码后三通道 ∈ [0,255]
- **Layer D 呈现层**（D1–D5）：BMP 头字段、行填充 4 字节对齐、BGR+bottom-up 回读还原、`np.rot90` 数学恒等、numpy 向量化解码与逐像素参考实现逐字节一致
- **Layer E 端到端**（E1–E2）：完整管线误差落在量化界内、连续 N 帧传输后同步器精确切出 N 帧

## test_hw_integration.py — 真机验证（4 用例）

在真实 YD-RP2040 + OV7670 上断言 main 固件的三个可观测面：

1. **`test_register_readback_rgb565_mode`**：`'R'` 回读 17 寄存器，关键位证明
   RGB565 模式 — COM7(0x12)=0x14、COM15(0x40)=0xD0、PID=0x76、VER=0x73
2. **`test_cam1_frame_stream_valid_image`**：CAM1 帧头 W/H=320×240、载荷完整
   153600 B、统计特征 = 真实图像（>50% 非零字节 + >100 种取值）
3. **`test_slow_waveform_href_edges`**：`'S'` 400 样本 @100us，40ms 窗口内
   HREF >10 次翻转，证明 PIO 行门控采集链在真机工作
4. **`test_frame_rate_sanity`**：两帧间隔 ∈ (50ms, 2s) — CDC 瓶颈 ~4 FPS 量级，
   排除采集/发送死锁类 bug 量级

### 运行前提：板上必须烧 main 固件

⚠️ **固件版本前提**：硬件测试断言的是 `main` 分支的 CAM1 帧协议。若板上烧的
是 `impl/uvc` 分支（TinyUSB **UVC webcam**），视频走 UVC 视频接口、不走 CDC：

- `CAM1` 同步必然超时（无帧流）
- `'R'`/`'S'` 诊断命令虽保留，但寄存器为 YUV 配置（COM7=0x10、COM15=0xC0）

症状识别：macOS 系统信息里板子以 **"UVC Camera"** 出现 = 非 main 固件。

```bash
# 重烧 main 固件（MCP upload 被策略拦截时用 CLI）：
pio run -e rpipico -t upload --upload-port /dev/cu.usbmodemXXXX
```

### 串口读法（重要约束）

帧流 ~660 KB/s 持续到达。若主机逐字节 `read(1)` 慢读，USB CDC 缓冲填满，
板子 `Serial.write(frames, 153600)` 阻塞，`loop()` 卡在发帧 → `'S'`/`'R'`
命令**饿死**（实测：慢读时 8s 无响应；大块读时 0.05s 响应）。
因此 `SerialStream` 用大块 `read(4096)` + 内部缓冲 + `find()` 滑窗同步，
保证主机吞吐始终高于帧流。这是踩过坑的硬约束，改测试读法时务必保持。

## test_hw_bayer.py — raw bayer 真机验证（4 用例）

在真实 YD-RP2040 + OV7670 上断言 RAW_BAYER 固件的 CAM2 帧协议：

1. **`test_01_reg_readback_raw_bayer_mode`**：`'R'` 回读 17 寄存器，关键位证明
   raw bayer 模式 — COM7(0x12)=0x01（sensor raw）、COM15(0x40)=0xD0、PID=0x76；
   **必须先于任何 `'T'` 运行**（断言的是 init 后全窗值 VSTART=0x03/VSTOP=0x7B）
2. **`test_02_cam2_frame_stats`**：`'T'` upper → ack（DBG1+0xF9+0x00）；CAM2 帧头
   640×240、载荷完整 153600 B（1 byte/px）、统计特征 = 真实图像
3. **`test_03_cfa_separation`**：上/下半帧 `cfa_means` 三通道均值差 >4 —— 输出确为
   拜耳 CFA（灰度误配置的量化噪声 ≤ ~1，分离阈值区分二者）；采集结果缓存供 test_04 复用
4. **`test_04_stitch_demosaic_bmp`**：`stitch_halves` → (480,640)、`demosaic_bayer` →
   RGB uint8、`rgb_to_bmp` 头/尺寸公式（54 + 行填充对齐后 480 行）、落盘字节与内存一致

### 固件模式探针（两个硬件文件按 COM7 自选）

同一块板一次只能烧一种固件。两个硬件测试文件在 `setUpClass` 里用 `'R'` 探针读 COM7
自动互选，**不假装通过也不报错**：

- `test_hw_integration.py`：COM7=0x01（raw bayer 固件）→ 整类 skip（CAM1/RGB565 断言不适用）
- `test_hw_bayer.py`：COM7≠0x01（非 raw bayer 固件）→ 整类 skip（CAM2 断言不适用）

探针失败（无 DBG1 回复、marker 不符）同样 skip —— 类级 setup 永不 fail。
无板时两个文件均整类 skip（49 纯软件用例照常通过）。
