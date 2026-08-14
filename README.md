# OV7670 摄像头 → RP2040 USB 实时视频流

YD-RP2040 无 FIFO OV7670 摄像头采集，通过原生 USB CDC 输出连续 RGB565 帧流。
固件用 PIO 采集 + DMA 搬运，主机端 Python 工具负责解析、存图与实时显示。

## 特性

- **无 FIFO 直采**：PIO0 SM1 按 PCLK/HREF 采样 8-bit 像素总线，VSYNC 上升沿触发 DMA 整帧搬运
- **连续帧流**：`loop()` 通过 USB CDC 背靠背发送 `"CAM1"` 帧（QVGA 320×240 RGB565）
- **实时查看器**：`live_view.py`（numpy 向量化解码 + pygame 显示，支持缩放/旋转；
  自动识别 `"CAM1"` RGB565 与 `"CAM2"` raw bayer 两种帧流）
- **单帧捕获**：`capture.py` 落盘 BMP（零依赖，随处可看）
- **板载诊断**：`'R'` 寄存器回读、`'W'/'S'` 波形采样、`'B'` 软重启进 BOOTSEL（免按键刷固件）
- **性能**：QVGA @ ~4.2 FPS（USB CDC 全速 12 Mbps 是瓶颈，~660 KB/s 有效吞吐）

## Branch Strategy

| Branch | Purpose |
|--------|---------|
| `main` | Reference implementation (PIO + DMA capture, CDC "CAM1" protocol) |
| `impl/uvc` | Alternative implementation — native USB UVC webcam (TinyUSB), incompatible with CDC "CAM1" protocol, **not merged into main** |
| `exp/*` | Experimental prototypes (disposable) |

> ⚠️ `impl/*` branches are intentionally not merged into `main`.
> They represent alternative design choices and are maintained separately.
> Pull requests from `impl/*` into `main` will generally be closed
> unless explicitly discussed.

## 硬件接线

| OV7670 | YD-RP2040 | 物理 Pin | 说明 |
|--------|-----------|----------|------|
| 3V3 | 3V3 | 36 | 3.3V，勿接 5V |
| GND | GND | 3/8/13… | 共地 |
| D0–D7 | GP8–GP15 | 11/12/14/15/16/18/19/20 | PIO IN bit0–7（`in_base=8`，避开 USB GP0/GP1） |
| PCLK | GP18 | 24 | PIO 输入，wait 边沿 |
| HREF | GP17 | 22 | PIO 输入，行门控 |
| VSYNC | GP16 | 21 | GPIO 输入，帧起止 |
| XCLK | GP22 | 29 | 硬件 PWM 20.83 MHz（125 MHz/6） |
| SIOC | GP21 | 27 | 硬件 I2C0 SCL ~10 kHz，+4.7k 上拉 |
| SIOD | GP20 | 26 | 硬件 I2C0 SDA ~10 kHz，+4.7k 上拉 |
| RESET | 3V3 | 36 | 常高 |
| PWDN | GND | — | 低电平 = 工作 |

完整表格见 `pinout.csv`（其中 XCLK 频率以本文为准：125/6 = **20.83 MHz**，低于 24 MHz 规格）。

## 接线图与实物

**理论接线图**（Fritzing 面包板图）：

![OV7670-RP2040 理论接线图](ov7670_rp2040_bb.jpg)

原始可编辑工程文件：[ov7670_rp2040.fzz](ov7670_rp2040.fzz)（用 Fritzing 打开，可改线再导出）。

**实际接线照片**：

![实际接线图照片](wiring_diagram_photo.jpg)

**成品实物图**：

![成品实物图](real_product.jpg)

## 工作原理

```
OV7670 ──8-bit D[0:7]──▶ PIO0 SM1 ──▶ DMA ──▶ frames[153600]
          PCLK/HREF       (像素采样)        (单缓冲)      │
VSYNC ──▶ GPIO IRQ ──▶ 触发 DMA ────────────────────────┘
                                                       ▼
USB CDC (Serial) ◀── loop() 发送 "CAM1" + W/H + RGB565 ◀─┘
```

1. **XCLK**：GP22 硬件 PWM 20.83 MHz（`-DXCLK_PWM`，另支持 PIO 8.3 MHz 默认档）
2. **采集**：PIO0 SM1 在 PCLK 边沿采样 GP8–GP15，HREF 高电平期间收像素字节
3. **DMA**：VSYNC 上升沿 IRQ 重新配置并触发 DMA，整帧搬入 `frames[]`（153,600 B）
4. **发送**：DMA 完成后置 `frame_ready`，`loop()` 经 USB CDC 发出。单缓冲：采集与发送串行，`frame_ready` 在发送完成后才清除，避免覆盖在发缓冲（这就是 ~4.2 FPS 的瓶颈来源）

## 帧协议（USB CDC）

```
"CAM1" (4 B) | W (u16 BE) | H (u16 BE) | W×H×2 字节原始 RGB565
```

- **像素为大端**：OV7670 先出高字节，PIO 保持字节序。主机解析必须用
  `(b[i] << 8) | b[i+1]`；用小端 `b[i] | b[i+1] << 8` 会交换 R/B 并打乱 G。
- 诊断包用独立的 `"DBG1"` 魔数，`capture.py` 的帧同步循环直接放行，不会误匹配。

## Raw bayer 模式（实验）

`-DRAW_BAYER` 构建把 OV7670 切到 **sensor raw 8-bit**（COM7=0x01）并输出
VGA 640×480 拜耳 CFA 的 **上/下半帧**（各 640×240，`'T'` 命令切换窗口）：

```
"CAM2" (4 B) | W (u16 BE) | H (u16 BE) | W×H 字节原始 Bayer（1 byte/px）
```

- **帧协议**：`bayer_capture.py` 按 `"CAM2"` 魔数滑窗同步，`'T'` 上/下半帧
  各 153600 B，`stitch_halves` 拼回 640×480。
- **采集 CLI**（依赖 `pyserial`；纯函数层零依赖）：
  ```bash
  python3 bayer_capture.py --port /dev/cu.usbmodemXXXX --out bayer_frames --pairs 3 --bmp
  ```
  `--out`（默认 `bayer_frames/`）、`--pairs`（上/下帧对数，默认 3）、`--pattern`
  （CFA，默认 RGGB）、`--bmp`（顺带写去马赛克 640×480 BMP）。
- **固件开关**（`platformio.ini`）：`-DRAW_BAYER -DFRAME_W=640 -DFRAME_H=240`。
  - 默认 `[env:rpipico]` = shipped 表 + PIO 每 2nd PCLK 采样（T7 修复，
    全分辨率 640 不同字节/行，test 5/5）。
  - `[env:rpipico_official]` = 官方 Table 2-2 Sheet 3 寄存器表
    （`-DRAW_BAYER_OFFICIAL_REGS`）+ per-PCLK 采样（`-DRAW_BAYER_PER_PCLK`）——
    第 8 组 A/B 诊断 env：'C' 直测 640 边沿/行但仅 320 不同字节/行，
    证实 2 PCLK/byte 与寄存器配置无关（详见 `docs/RAW_BAYER_OPERATION_MATH.md`
    §10.5）；该 env 下 `test_03` 预期失败（dup_even=1.000）。
- ✅ `live_view.py` 已适配 raw bayer（CAM2）：自动交替发 `'T'` 收上/下半帧，
  拼接成 **640×480 完整画面**显示（灰度默认 / `--demosaic` 彩色），
  无信号超时 2s 显示白色 NO-SIGNAL 屏；`capture.py` 仍为 RGB565（CAM1）专用。
  `test_hw_integration.py`/`test_hw_bayer.py` 通过 COM7 探针自动选择对应固件用例。

## 构建与烧录

依赖：PlatformIO Core（平台 `maxgerhardt/platform-raspberrypi.git`，框架 `arduino` = arduino-pico）。

```bash
# 编译（QVGA 320×240）
pio run

# 烧录（自动探测端口；板子死机时先按住 BOOTSEL 上电，或先发 'B'）
pio run -t upload

# 串口监视
pio device monitor -b 115200
```

分辨率与时钟在 `platformio.ini` 的 `build_flags` 里调：

```ini
-DARDUINO_USB_MODE=1          ; 原生 USB，Serial = USB CDC（GP0/GP1 空闲给相机）
-DARDUINO_USB_CDC_ON_BOOT=1
-DFRAME_W=320 -DFRAME_H=240   ; 帧尺寸（live_view.py 自动适配任意 WxH）
-DXCLK_PWM                    ; XCLK 走硬件 PWM 20.83 MHz
```

> 注意：改分辨率需同步 `ov7670.c` 的缩放寄存器（当前 init 表为 QVGA 值）。
> 降到 160×120 可把帧率提到 ~15 FPS，但画面会变糊（详见下方"性能"）。

## 主机端用法

Python 依赖：`pyserial`（capture.py）、`numpy` + `pygame`（live_view.py）。
作者环境：PlatformIO 自带 Python 3.11（`~/.platformio/penv/bin/python3`）。

### 单帧捕获 → BMP

```bash
python3 capture.py [port] [outdir] [nframes]
# 例：抓 10 帧到 frames/
python3 capture.py /dev/cu.usbmodem141101 frames 10
```

### 实时查看

```bash
python3 live_view.py [port] [scale] [rotate] [--demosaic] [--frames N]
# 例：2 倍放大 + 向左旋转 90°（默认）
python3 live_view.py            # port=自动探测, scale=2, rotate=90
python3 live_view.py /dev/cu.usbmodem141101 2 0    # 不旋转
# raw bayer 固件（CAM2）：自动拼接 640×480 完整画面，--demosaic 彩色，--frames 6 退出
python3 live_view.py --demosaic --frames 6
```

按键：`S` 存当前帧（所见即所得 BMP），`T` 强制切换 CAM2 上/下半帧窗口
（CAM2 默认自动交替拼接，无需手动按），`Q`/`Esc` 退出。无信号时显示白屏。

## 诊断命令（串口发送单字符）

| 命令 | 功能 |
|------|------|
| `W` | 快速波形采样（GPIO 直读，不干扰采集流水线） |
| `S` | 慢速波形采样 |
| `R` | 寄存器回读：验证 init 写入 + 实时 AGC/AEC 状态（`DBG1` 包） |
| `T` | raw bayer 模式切换上/下半帧窗口（`-DRAW_BAYER` 构建） |
| `C` | PCLK 边沿直测：SM2 PIO 按行计数 PCLK 上升沿，`DBG1` 包回 4 行 u32 BE（T7 决定性测量，`-DRAW_BAYER` 构建） |
| `B` | 软重启进 BOOTSEL（U 盘模式拖放刷固件） |

板子无响应（收不到 `CAM1`、`R` 无 `DBG1` 回复）时先重新上电。

## 性能与已知限制

- **~4.2 FPS @ QVGA**（10 帧 2.386 s，~660 KB/s）。瓶颈是 USB CDC 全速有效吞吐，
  不是采集。要 5 FPS 需 768 KB/s，接近 12 Mbps 全速 USB 物理天花板。
- **双缓冲乒乓不可行**：2 × 153,600 = 300 KB 超过 RP2040 264 KB SRAM（`main.cpp` 已注明）。
- **降分辨率**：改 `FRAME_W/H = 160/120` + `ov7670.c` 缩放寄存器 → ~15 FPS，但细节变糊
  （OV7670 硬件整数倍缩放，非高质量插值）。主机端双线性放大可缓解块状感，救不回细节。
- 像素按大端解析是硬约束，改动前务必读 `capture.py` 里 `rgb565_to_bmp` 的注释。

## 目录结构

```
├── platformio.ini      # 构建配置（build_flags：USB/分辨率/XCLK）
├── pinout.csv          # 接线表
├── .gitignore          # 忽略构建输出（.pio/）、生成帧（frames/）等
├── ov7670_rp2040_bb.jpg   # 理论接线图（Fritzing 面包板图）
├── ov7670_rp2040.fzz     # Fritzing 原始工程文件（可编辑）
├── wiring_diagram_photo.jpg  # 实际接线照片
├── real_product.jpg        # 成品实物图
├── capture.py          # 单帧捕获 → BMP
├── live_view.py        # 实时查看器（numpy + pygame；CAM1 RGB565 + CAM2 raw bayer）
├── verify_frame.py     # 帧数据校验工具
├── docs/
│   ├── OV7670_RP2040_REFERENCE.md
│   ├── RAW_BAYER_OPERATION_MATH.md   # raw bayer 实验主线（T1–T7 结论 + 8 组寄存器配置 A/B + §10 归档）
│   └── git-branch-strategy.md
├── include/            # 头文件目录（PlatformIO 模板）
├── lib/                # 私有库目录（PlatformIO 模板）
├── test/               # 测试：纯软件数学验证 + 硬件集成验证（见 test/README.md）
└── src/
    ├── main.cpp        # 主逻辑：PIO 采集 + DMA + USB 流 + 诊断
    ├── camera.pio      # PIO 程序（XCLK + 捕获）
    ├── camera.pio.h    # pioasm 生成的头文件
    ├── ov7670.c/.h     # OV7670 驱动（SCCB over 硬件 I2C0）
```

## 参考

- [docs/OV7670_RP2040_REFERENCE.md](docs/OV7670_RP2040_REFERENCE.md) — 技术参考（寄存器、时序、调试记录）
- OV7670 数据手册（SCCB / RGB565 / QVGA 寄存器表）
