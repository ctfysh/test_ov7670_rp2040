# OV7670 无 FIFO 直采 → RP2040：完整操作流程与数学原理

> 版本：2026-08-14（T7 验证完成，水平 2x 重复缺陷根因已确认并修复）
> 适用分支：`exp/raw-bayer`（RAW_BAYER 实验）；RGB565 基线见 `main`。
> 本文档 = 操作手册 + 数学原理 + 缺陷取证全记录。公式一律用 LaTeX。

---

## 目录

1. [项目概览](#1-项目概览)
2. [硬件平台与接线](#2-硬件平台与接线)
3. [固件架构与数据通路](#3-固件架构与数据通路)
4. [帧协议（CAM1 / CAM2 / DBG1）](#4-帧协议cam1--cam2--dbg1)
5. [Raw Bayer 模式与窗口机制](#5-raw-bayer-模式与窗口机制)
6. [水平 2x 重复缺陷：诊断与根因（T7 全记录）](#6-水平-2x-重复缺陷诊断与根因t7-全记录)
7. [数学原理](#7-数学原理)
8. [验证体系（纯软件 + 硬件）](#8-验证体系纯软件--硬件)
9. [操作手册](#9-操作手册)
10. [结果与证据](#10-结果与证据)
11. [附录](#11-附录)

---

## 1. 项目概览

在 **YD-RP2040**（RP2040 双核 Cortex-M0+，264 KB SRAM，无 FIFO）上直采
**OV7670** CMOS 摄像头（VGA 640×480，SCCB/I2C 控制，无内部 FIFO），
经原生 USB CDC 输出连续帧流。本实验分支（`exp/raw-bayer`）把传感器切到
**sensor raw 8-bit Bayer** 模式（COM7=0x01），输出 VGA 分辨率的拜耳 CFA
原数据，验证"原始拜耳采集 → 主机端去马赛克 → BMP 呈现"全链路。

**目标**（原始计划 Tasks 1–7）：

- 纯软件数学层零依赖、可在 CI 无板运行（去马赛克、帧协议、BMP）；
- 硬件层在真机上断言：寄存器回读、CAM2 帧流、CFA 分离、缝合去马赛克；
- **解决 raw 模式下 100% 水平 2x 字节重复缺陷**（T7，根因确认并修复）。

**核心结论速览**：

| 项 | 值 |
|---|---|
| 根因 | raw 模式每个不同字节在 PCLK 总线上保持 2 个周期（1280 PCLK/行，640 个不同字节） |
| 修复 | PIO 程序**每 2 个 PCLK 上升沿采样一次**（camera.pio 6 指令环） |
| 效果 | `dup_even`：1.000 → **0.385**；Bayer 同面相关签名在 640 宽清晰（colLag2=0.426 >> colLag1=0.146） |
| 结论 | **不是寄存器可修**——8 组寄存器配置实测均无效；FRAME_W=640 保持，无几何改动 |

---

## 2. 硬件平台与接线

### 2.1 器件

- **MCU**：RP2040（双核 133 MHz，264 KB SRAM，PIO0/PIO1，USB 控制器）。
- **摄像头**：OV7670（OmniVision，VGA 640×480，SCCB 控制，无 FIFO，D0–D7 并行输出）。
- **注意**：无 FIFO 意味着主机必须**实时**按 PCLK/HREF 采样数据总线，
  这是整个 PIO 采集方案存在的原因。

### 2.2 引脚映射（USB 安全，`IN_BASE` 必须是 8 的倍数）

| OV7670 | YD-RP2040 | 物理 Pin | 用途 |
|---|---|---|---|
| D0–D7 | GP8–GP15 | 11/12/14/15/16/18/19/20 | PIO IN bit0–7（`in_base=8`，避开 USB GP0/GP1） |
| PCLK | GP18 | 24 | PIO 输入，`wait` 边沿 |
| HREF | GP17 | 22 | PIO 输入，行门控 |
| VSYNC | GP16 | 21 | GPIO 输入，帧起止 |
| XCLK | GP22 | 29 | 硬件 PWM 20.83 MHz（125/6） |
| SIOC | GP21 | 27 | 硬件 I2C0 SCL ~10 kHz，+4.7k 上拉 |
| SIOD | GP20 | 26 | 硬件 I2C0 SDA ~10 kHz，+4.7k 上拉 |
| RESET | 3V3 | 36 | 常高 |
| PWDN | GND | — | 低电平 = 工作 |

> 关键约束：`IN_BASE` 必须为 8 的倍数；`wait` 用**绝对 GPIO 号**
> （`wait 1 gpio 17/18`），不能用 `wait pin`（那会相对 IN base 偏移，打到 D0/D1）。

### 2.3 时钟

$$f_{\text{XCLK}} = \frac{f_{\text{sys}}}{6} = \frac{125\ \text{MHz}}{6} \approx 20.83\ \text{MHz}$$

低于 OV7670 规格上限 24 MHz；CLKRC=0x80（fINT = XCLK/2）时
传感器内部主时钟：

$$f_{\text{INT}} = \frac{f_{\text{XCLK}}}{2} \approx 10.4\ \text{MHz}$$

---

## 3. 固件架构与数据通路

```
OV7670 ──8-bit D[0:7]──▶ PIO0 SM1 ──▶ DMA ──▶ frames[153600]
          PCLK/HREF       (像素采样)        (单缓冲)      │
VSYNC ──▶ GPIO IRQ ──▶ 触发 DMA ────────────────────────┘
                                                       ▼
USB CDC (Serial) ◀── loop() 发送 "CAM1"/"CAM2" + W/H + 载荷 ◀─┘
```

1. **XCLK**：GP22 硬件 PWM 20.83 MHz（`-DXCLK_PWM`）。
2. **采集**：PIO0 SM1 在 PCLK 边沿采样 GP8–GP15；HREF 高电平期间收字节。
3. **DMA**：VSYNC 上升沿 IRQ 重新配置并触发 DMA，整帧搬入 `frames[]`。
4. **发送**：DMA 完成后置 `frame_ready`，`loop()` 经 USB CDC 发出。
   单缓冲：采集与发送串行（这就是帧率瓶颈来源，见 §7.9）。

### 3.1 PIO 捕获程序（camera.pio）

`-DRAW_BAYER` 构建使用**每 2 个 PCLK 采样一次**的 6 指令环：

```pioasm
.program capture
.wrap_target
    wait 1 gpio 17     ; HREF 高 -> 行开始
    wait 1 gpio 18     ; PCLK 上升沿（跳过：字节保持 2 个 PCLK）
    wait 0 gpio 18     ; PCLK 下降沿
    wait 1 gpio 18     ; 下一个上升沿 -> 在此采样
    in pins, 8         ; 采样 D0-D7（in_base = GP8..GP15）
    wait 0 gpio 18     ; PCLK 下降沿（保证循环以 LOW 结束）
.wrap
```

**循环必须以 PCLK LOW 结束**：否则循环开头的 `wait 1 gpio 18` 会在当前高电平
上"直接穿过"，跳过下一个上升沿，破坏采样相位（这是本程序的关键时序约束）。

### 3.2 寄存器初始化（ov7670.c）

关键寄存器（best-known 配置，test_01 回读断言；与已验证的 RGB565 缩放块一致）：

| 寄存器 | 值 | 含义 |
|---|---|---|
| COM7 (0x12) | 0x01 | sensor raw（8-bit Bayer） |
| COM15 (0x40) | 0xD0 | 全 0–255 输出范围 |
| CLKRC (0x11) | 0x80 | fINT = XCLK/2（20.83 MHz 构建） |
| DBLV (0x6B) | 0x0A | PLL |
| MVFP (0x1E) | 0x07 | 不翻转 |
| COM14 (0x3E) | 0x18 | bit4+bit3 打开 0x73 门；bits[2:0]=000 PCLK /1 |
| SCALING_XSC (0x70) | 0x00 | 缩放旁路（与 RGB565 路径一致） |
| SCALING_YSC (0x71) | 0x00 | 缩放旁路 |
| SCALING_DCWCTR (0x72) | 0x00 | 不下采样（默认 0x11 = HDS by 2） |
| SCALING_PCLK_DIV (0x73) | 0x08 | bit[3]=1 旁路分频器 |
| REG74 (0x74) | 0x20 | 水平缩放比 1x（Table 6-1） |
| VSTART (0x19) / VSTOP (0x1A) / VREF (0x03) | 0x03 / 0x7B / 0x03 | 全窗 VGA (15, 492) |

---

## 4. 帧协议（CAM1 / CAM2 / DBG1）

### 4.1 CAM1（RGB565 基线，main 分支）

```
"CAM1" (4 B) | W (u16 BE) | H (u16 BE) | W×H×2 字节原始 RGB565
```

- 像素**大端**：`(b[i] << 8) | b[i+1]`。用小端解析会交换 R/B 并打乱 G。

### 4.2 CAM2（raw bayer 实验，exp/raw-bayer 分支）

```
"CAM2" (4 B) | W (u16 BE) | H (u16 BE) | W×H 字节原始 Bayer（1 byte/px）
```

- RAW_BAYER 构建下帧尺寸：`FRAME_W=640, FRAME_H=240`（半帧），
  `FRAME_BYTES = FRAME_W × FRAME_H`（×1，8-bit Bayer）——不是 ×2。

### 4.3 DBG1（诊断包）

- `'R'` 回读 24 寄存器，关键位证明模式（COM7=0x01 → raw bayer）。
- `'T'` 窗口切换 ack：`DBG1 + 0xF9 + 0x00/0x01`（0xFF = SCCB 写失败）。
- `'S'/'W'` 波形采样；`'B'` BOOTSEL 软重启（测试永不发送）。

---

## 5. Raw Bayer 模式与窗口机制

### 5.1 为什么要半帧

- RAW_BAYER 输出 VGA 640×480，**1 byte/px**，整帧 307,200 B。
- 但 `FRAME_H` 编译为 240（半帧），通过 `'T'` 命令在**上/下窗口**间切换，
  每次采集 640×240 = 153,600 B。理由：单缓冲 SRAM 预算 + 让主机侧可
  滑动窗口同步 + 逐半统计（验证杠杆）。
- `stitch_halves` 把上下半帧 `np.vstack` 拼回 640×480。

### 5.2 窗口寄存器数学（datasheet §6.5, p.48–53）

传感器窗口由 4 个寄存器编码，**位域重叠**：

$$VSTRT = (0x19 << 2)\ |\ VREF[1:0]$$
$$VSTOP = (0x1A << 2)\ |\ VREF[3:2]$$
$$HSTART = (0x17 << 3)\ |\ HREF[2:0]$$
$$HSTOP = (0x18 << 3)\ |\ HREF[5:3]$$

实际窗口值（已验证）：

| 窗口 | VSTRT | VSTOP | 0x19 | 0x1A | VREF 低半字节 |
|---|---|---|---|---|---|
| 全窗 VGA | 15 | 492 | 0x03 | 0x7B | 0x03 |
| 上半 | 15 | 255 | 0x03 | 0x3F | 0x0F |
| 下半 | 252 | 492 | 0x3F | 0x7B | 0x00 |

> **VSTOP 排他语义（2026-08-14 实测）**：窗口 (VSTRT, VSTOP) 实际输出
> 行 VSTRT..VSTOP-1。旧上窗 VREF[3:0]=0x3（VSTOP_eff=252）只交付 237 行
> （15..251），捕获的第 237..239 行是**下一帧顶部行回绕**（corr 0.76–0.85
> 对上窗前 3 行），并非设计假设的"重叠 252..254"——这是亮度缝的根因
> （亮回绕行被混进暗下窗的 seam）。修复：上窗 VREF[3:0]=0x0F →
> VSTOP_eff=255 → 行 15..254（240 行），与下窗形成真实 3 行重叠（252..254）。

**VREF 读改写**：保留 bits[7:4]（AGC 增益高位）：

$$VREF_{\text{new}} = (VREF_{\text{read}} \ \&\ 0xF0)\ |\ VREF_{\text{lo}}$$

**TSLB[0]=0（reg 0x3A）必须在任何窗口切换前写入。**

### 5.3 已知伪影

上窗行 15..254、下窗行 252..491 → **3 行重叠（252..254）**，是窗口寄存器
方案的已知伪影；重叠行在两半帧中内容一致（曝光已锁定），vstack 不去重、
重叠行重复一次为已知伪影；逐半统计才是验证杠杆（`stitch_halves` 纯 vstack）。

### 5.4 旋转伪影（旧固件）与修复验证（2026-08-14）

**旧固件症状**：v2 采集中 6/12 坏帧为**行级旋转**（+220/+39/+11 行循环移位，
corr 0.75–0.95 vs shift-0 的 0.10–0.14）。坏帧行级字节对齐 corr~1.0 →
真实行级旋转，非字节流伪影。

**v2 环分析（推翻旧结论）**：v2 环 153/153 边沿 HREF=0、PC=4（SM 卡在
autopush `in pins,8`）、FIFO=0（2-bit 编码，4 words 满回绕为 0）→ **v2 实际
运行在 FULL-window 模式**：DMA 收满 240 行后传感器仍输出 477 行，SM 继续
采样 → FIFO 满 → 停 autopush。此前"v2 是半窗口伪影"的结论**错误**，
真实机制是**旧上窗数学 + 缺失 AGC/AEC 锁定**（见下）。

**根因链**（修复于 §5.2 VSTOP 排他语义）：
1. 旧上窗 VREF[3:2]=0b00 → VSTOP_eff=252 → 只交付 237 行（15..251）；
2. 捕获 DMA 配额是 240 行 → 第 237..239 行 = **下一帧顶部 3 行回绕**
   （corr 0.76–0.85 对上窗前 3 行）→ 亮度缝 + 行内容错位；
3. AGC/AEC 在窗口切换后重新收敛 → 缝两侧曝光跳变
   （修复前实测 upper R≈148.6 vs lower R≈42.1）。

**修复**：上窗 VREF[3:2]=0b11 → VSTOP_eff=255 → 行 15..254（240 行，真实
3 行重叠 252..254）；COM8=0xE1（AGC/AEC 冻结，300 ms 收敛后锁定）。

**陈旧字机制**（已知、非缺陷）：DMA 完成后 SM 继续采样 → FIFO 满（4 words）
→ 停 autopush；下次 arming 的 clear_fifos 使陈旧 4 B 先入 FIFO → 每帧首
4 字节=陈旧字（first4=`00000000`），仍行对齐，行均值不可见。

**修复验证（当前固件，设备寄存器实测）**：
- `'R'` 回读确认已烧录：COM7=0x01、VREF=0x0F、VSTRT/VSTOP=0x03/0x3F、COM8=0xE1。
- 全窗口 framesave_probe：**40/40 GOOD**（shift-0 corr 0.997–1.000）。
- 半窗口 framesave_half：**40/40 GOOD**；vring_probe **12/12 GOOD**
  （940 个 guard 边沿全部 HREF=0 → arming 从不发生在窗口中途 → 旋转
  结构上不可能；PC=0 干净停靠与 PC=4 FIFO 满停滞两种状态都产出对齐帧）。
- 端到端 `bayer_capture.py --pairs 3 --bmp`：3/3 对 OK，seam 重叠行 corr
  0.908–0.947，3 张 640×480 BMP 有效。
- 两模式累计 **92/92 GOOD**，旋转不再复现。

### 5.4.1 旋转伪影 · 第二根因：VSYNC multi-fire 的 hblank 误武装（2026-08-14 晚）

**新症状**：§5.4 修复后仍出现间歇性旋转坏帧（seam≈0、行级旋转 +K 行带
WRAP，fr[r]≈ref[(r+off)%240]，off 逐帧漂移 +45..+89）。clean 态完全正常、
multi-fire 态 100% 旋转 → 坏帧只在 VSYNC 线突发（multi-fire）期间出现。

**'V' 环决定性证据（修订旧结论）**：旧结论"所有 arming 边沿 HREF=0/PC=0
→ 对齐干净，旋转必来自传感器"是**错误的**——hblank（行间 13.6 µs HREF
低电平）同样满足 HREF=0/PC=0，而 multi-fire 的杂散边沿就落在 hblank 里，
恰好通过旧守卫 `!frame_ready && !dma_busy` → DMA 从窗口第 K 行起采 240
行 → 旋转。'V' 环 1024 边沿 multi-fire 态 bursts>900（1024 边沿里近 900
个独立突发），clean 态 bursts=1。

**时序实测（'W'/'S' 波形包，50 ns/样本）**：
- hblank = 13.6 µs（745/746 个 HREF-low run 完全一致）→ 旧守卫无法区分
  hblank 与帧间隔；
- 真实帧边沿（VSYNC 上升沿）位于 HREF-low 已持续 **700–800 µs** 处
  （'S' 10/10 帧一致，min=700/median=701/max=800 µs）→ vblank 中段；
- vsync 上升沿宽度 200 µs；帧周期 564 ticks@64 µs = 36.1 ms = 27.7 fps。

**修复（vblank-gated arming，`VBLANK_GATE_US=512`）**：`vsync_isr` 增加
HREF 下降沿 IRQ 记录 `last_href_fall_us`；VSYNC 上升沿只有满足
`HREF-low >= 512 µs` 才允许武装 DMA（ring bit7 = guard && vblank_ok）。
阈值安全窗：高于 hblank 38×、低于真实边沿最小 elapsed（700 µs）1.37×。
hblank（13.6 µs）与行有效（HREF=1）永不通过；真实帧边沿（700–800 µs）
恒通过。**vblank 内任意时刻武装都安全**：窗口首行尚未开始，捕获 SM 的
`wait 1 gpio 17` 电平等待保证从窗口第 1 行起采。

**修复验证（当前固件，multi-fire 最坏态）**：
- **'V' 环不变量 PASS**：575/575 arming 边沿全部 HREF=0（vblank 内）；
  multi-fire 态（bursts=1023/1024、gaps>500=909）下 ring 仍然全绿 →
  中途武装结构上不可能。
- **seam 回归**：U1[-3:]~L1[:3] = **+0.984**（4 组合全 0.983–0.984，
  优于旧固件 clean 态 +0.89）。
- **wrap 测试**：u1/u2 row0~row239 corr = 0.22–0.24（无旋转）。
- **与修复前同场景对照**：fresh vs 17:00 修复前已知 GOOD 帧
  （`exp_clean/u1.npy`）off=0 corr 0.963/0.784 → 对齐完全保留。
- **'H' 行为**：`lines/frame` 用 RAW vsync 边沿计数（armed 计数因 1 s
  忙等窗口阻塞 loop() 而不可用——窗口内 loop() 不消费帧 → 守卫常闭 →
  实测 armed≈1）。multi-fire 期 lines/frame 读数偏低（<477）即垂直
  时序不稳信号；ring bit7 独立证明这些边沿从未中途武装 → 无旋转。
- 自洽：U1~U2=0.973、L1~L2=0.972（同窗口帧间一致）。

---

## 6. 水平 2x 重复缺陷：诊断与根因（T7 全记录）

这是本实验最重要的部分——一个"100% 确定性水平复制"缺陷如何被定位、
排除寄存器假设、最终在 PIO 层修复。

### 6.1 缺陷签名

每半帧中，**偶数/奇数相邻字节逐位 100% 相等**：

$$I(y,\ 2x) = I(y,\ 2x+1) \quad \forall\ y,\ x \quad \Rightarrow \quad \text{dup\_even} = 1.000$$

- 正常内容下相邻字节是**不同滤色器像元**（R–G / G–B），只在平滑区偶发相等，
  远低于 1.0 → 阈值 0.9 双侧留足裕量。
- 4 帧实测全行 `dup_even = 1.000`（确定性，非噪声）。

### 6.2 八个被证伪的寄存器候选

> 每一组配置都**实测**（改寄存器 → 重烧 → 采集 → 算 dup_even），全部保持 1.000。

| # | 候选 | 理论依据（当时） | 实测结果 |
|---|---|---|---|
| 1 | XSC/YSC = 0x00 | 关掉 DSP 水平缩放 | dup_even = 1.000 |
| 2 | PCLK_DIV = 0xF0 | 使能分频器 /2 | dup_even = 1.000 |
| 3 | DCWCTR = 0x00 | 关闭 HDS by 2 | dup_even = 1.000 |
| 4 | COM14 = 0x18 | 打开 0x73 门 | dup_even = 1.000 |
| 5 | PCLK_DIV = 0x08 | bit3=1 旁路（"上一轮修复"） | dup_even = 1.000 |
| 6 | REG74 = 0x20 | 水平缩放比 1x（Table 6-1） | dup_even = 1.000 |
| 7 | 最小寄存器表 | CLKRC=0x01 + COM14=0x08 + DCWCTR=0x11 + 0x73=0x00 | dup_even = 1.000 |
| 8 | 官方 Table 2-2 Sheet 3 全表 | datasheet 官方 raw 参考配置（`RAW_BAYER_OFFICIAL_REGS`） | dup_even = 1.000（per-PCLK 采样）；'C' 直测 640 边沿/行但仅 320 个不同字节/行（§10.5） |

**结论：不是寄存器可修的。** 所有缩放相关寄存器都无法影响输出模式；
同帧捕获路径与已知可用的 RGB565 worktree 逐字节一致（排除固件采集 bug）。
第 8 组进一步表明：即使官方缩放配置把边沿数降到 640/行，行内不同字节也
同步减半到 320（DCWCTR=0x11 HDS by 2）→ 每字节仍保持 2 PCLK，与配置无关。

### 6.3 两个竞争假设

- **假设 A（320 宽）**：行只有 320 个不同字节，每个输出 2 次
  （XSC/YSC=0x3A/0x35 → DSP 1/2 缩放的残留）。→ 修复应在寄存器。
- **假设 B（640 宽，2 PCLK/字节）**：行有 640 个不同字节，但传感器
  **每个字节在 PCLK 总线上保持 2 个周期**（1280 PCLK/行）。
  → 修复应在采样侧（PIO 每 2nd PCLK 采样）。

datasheet Table 6-3 把 PCLK/byte 绑定到水平缩放因子段
（1x..1/2x → 1 PCLK/byte；1/2x..1/4x → 2 PCLK/byte）。**该表不适用于本项目
raw 配置**：REG74=0x20 → `0x20/0x20` = 1x，按表落在 1x..1/2x 段 → 应为
1 PCLK/byte（早前文档把 2 PCLK/byte 归因于 1/2x..1/4x 段，是错误的，已更正）。

官方 raw 时序（datasheet Table 3-3）：VGA Bayer RGB 的 PCLK = fINT/2
（同为 fINT=24MHz 时 YUV PCLK=24MHz、Bayer PCLK=12MHz）→ 640 像元行 =
**640 PCLK/行、1 byte/PCLK**。Linux 驱动（drivers/media/i2c/ov7670.c）印证：
`fps = 5/2*pixclk for RAW`（YUV/RGB 为 5/4），并在 set/get_framerate 中对
SBGGR8 做 `clkrc<<1`/`clkrc>>1` —— 驱动模型里 RAW 的 PCLK 有效频率即
fINT/2。另：该驱动注释记录 datasheet 声称 clkrc=0 时 XCLK 除 1，但实测
（示波器）是除 2 —— 芯片存在 datasheet 未记载的行为偏差，与本次 2 PCLK/byte
疑点同理，属"实测为准"的佐证。

**结论（修正后）**：2 PCLK/byte 是**实测现象**（8 组配置 dup_even=1.000 +
RGB565 路径对照排除采样链缺陷）。**判定性实验已完成**（'C' 命令 + SM2 PIO
计数器，2026-08-14 直接计数）：shipped 配置 **1280 边沿/行**（见 §6.4
末 + §10.5）——"1280 PCLK/行"从推断变为直接测量，2 PCLK/byte 成立；
datasheet 的 640 PCLK/行时序仅适用于官方缩放配置（其输出行也只有 320 个
不同字节，见 §10.5 第 8 组 B），两种配置下每字节都保持 2 PCLK → **与寄存器
配置无关**。

### 6.4 判别实验：空间相关签名（决定性证据）

对采集帧按**不同宽度重塑**并计算**同面（same-plane）列相关**：

$$\rho_k^{\text{col}} = \text{Pearson}(I(y, x),\ I(y, x+k)) \quad\text{（同色平面采样）}$$

**Bayer 同面相关签名**（数学上必须成立的性质）：

- **干净 640 宽**：同色像元相距 2 列 → colLag2 ≫ colLag1
  （G 平面相邻采样同列不相关，隔一列强相关）。
- **错误的 320 宽**：重塑把不同色像元混进同一"逻辑列" → 签名消失。

实测（修复后）：

| 重塑宽度 | colLag1 | colLag2 | rowLag2 | 判定 |
|---|---|---|---|---|
| **640** | 0.146 | **0.426** | 0.442 | ✅ 干净 Bayer 签名 |
| 320 | — | — | — | ❌ 无该签名 |

- 640 宽 colLag2=0.426 >> colLag1=0.146：**假设 B 获胜**，Bayer 结构完整。
- 若假设 A（320 宽）为真，640 重塑下每行只有 320 个独立采样，
  修复应输出 320 采样/行 → 签名在 320 重塑清晰、640 重塑混乱。实测相反。

### 6.5 修复

PIO 程序改为**每 2 个 PCLK 上升沿采样一次**（camera.pio 6 指令环，见 §3.1）。
每个字节保持 2 个 PCLK → 跳过第 1 个上升沿、在第 2 个上升沿采样，
恰好恢复全部 640 个不同字节。

**修复后验证**：

- `dup_even`：1.000 → **0.385**（正常场景的偶发相等水平）。
- 640 宽干净 Bayer 签名（colLag2=0.426 >> colLag1=0.146）。
- 320 重塑无该签名 → **FRAME_W=640 保持，无几何改动**。
- 跨帧相关：NEW upper ≈ 0.66 / lower ≈ 0.87（OLD 0.29/0.44）——更稳定。
- 全量硬件测试 4/4 PASS（test_01 寄存器回读 / test_02 帧统计 /
  test_03 dup_even<0.9 / test_04 缝合+BMP）。

### 6.6 为什么寄存器值保持"best-known"而不是 revert

8 组配置都无效说明这些寄存器**既不致病也不治病**；但保持当前值
（缩放旁路、与 RGB565 路径一致）能保证两条路径行为一致，且
test_01 回读断言它们——任何未来改动必须同步更新测试。

---

## 7. 数学原理

### 7.1 Bayer CFA 结构

CFA 是 2×2 重复块（本项目默认 **RGGB**，datasheet §6.1）：

$$P = \begin{bmatrix} R & G \\ G & B \end{bmatrix}$$

坐标 $(y,x)$ 处的颜色（$y,x$ 从 0 开始）：

$$c(y,x) = P\big[(y \bmod 2)\cdot 2 + (x \bmod 2)\big]$$

即：偶行偶列 = R，偶行奇列 = G，奇行偶列 = G，奇行奇列 = B。

### 7.2 去马赛克（bayer_demosaic.py）

#### 7.2.1 双线性 G-first

**G 通道**：在 R/B 位置用 4 个正交 G 邻居平均：

$$G'(y,x) = \frac{G(y{-}1,x) + G(y{+}1,x) + G(y,x{-}1) + G(y,x{+}1)}{4}$$

**R 通道**：在缺失位置用同色 8-邻域平均（RGGB 几何退化为）：
- G 位置的 R = 左右共线 2 邻居平均；
- B 位置的 R = 对角 4 邻居平均。

**G 校正（色彩保持）**——关键的非线性步骤：

$$R'(y,x) = \bar R_8(y,x) \cdot \frac{G(y,x)}{\bar G_8^{R}(y,x)}$$

其中 $\bar R_8$ 是同色 R 邻域均值，$\bar G_8^{R}$ 是**在 R 邻居位置上的 G 均值**。
B 通道对称。该校正使去马赛克在 G 与 R/B 比值上保持局部色彩，避免
色边（color fringe）。对线性/常量 CFA **数学精确**（测试向量可逐像素断言）。

**边界处理**：只用界内同色采样（valid-mask），无 padding，无环绕贡献。

#### 7.2.2 最近邻（硬件取证）

确定性 2×2 块复制（行主序 tie-break），$b_y = y \ \&\ \sim1,\ b_x = x \ \&\ \sim1$：

$$I_{\text{out}}(y,x) = \big(cfa[b_y,b_x],\ cfa[b_y,b_x{+}1],\ cfa[b_y{+}1,b_x{+}1]\big)$$

要求 $h,w$ 均为偶数。快速预览/缺陷取证用。

### 7.3 缺陷检测度量：dup_even

$$d = \frac{1}{H \cdot (W/2)} \sum_{y=0}^{H-1} \sum_{x=0}^{W/2-1}
\left[ I(y, 2x) = I(y, 2x+1) \right]$$

- 缺陷签名 = 1.000（确定性复制）；正常内容远低于 0.9。
- 阈值 **0.9**：双侧留足裕量；平滑区偶发相等不会越过。
- 全黑/全灰平场由 test_02 的 `>100 种取值` 断言前置排除（避免假阴性场景）。

### 7.4 空间相关签名分析

对同色平面（如 G 平面）计算 Pearson 列相关：

$$\rho_k = \frac{\sum_{y,x} (I(y,x)-\bar I)(I(y,x{+}k)-\bar I)}
{\sqrt{\sum_{y,x}(I(y,x)-\bar I)^2}\ \sqrt{\sum_{y,x}(I(y,x{+}k)-\bar I)^2}}$$

Bayer 性质：同色像元列距为 2 → $\rho_2 > \rho_1$。这是 6.4 判别实验的数学基础。

### 7.5 RGB565 位域数学（CAM1 基线）

大端字节对解码：

$$v = (b[i] \ll 8)\ |\ b[i{+}1]$$

位域提取与 8-bit 扩展（全遍历验证）：

$$R_8 = \frac{R_5 \cdot 255}{31}, \qquad
G_8 = \frac{G_6 \cdot 255}{63}, \qquad
B_8 = \frac{B_5 \cdot 255}{31}$$

或等价的移位近似：$r_5\cdot 8 + (r_5 \gg 2)$ 等。测试全遍历 0..31 / 0..63
验证值域 + 严格单调 + 端点。

### 7.6 BMP 格式数学

24bpp 无压缩，BGR，自底向上：

$$\text{row\_size} = (W \times 3 + 3)\ \&\ \sim 3 \quad\text{（4 字节对齐）}$$
$$\text{data\_size} = \text{row\_size} \times H$$
$$\text{file\_size} = 54 + \text{data\_size}$$

- 头：14 B BITMAPFILEHEADER（`BM` + file_size + 54 偏移）+ 40 B BITMAPINFOHEADER。
- 行序：$y = H{-}1 \to 0$ 写入；每行尾补 0 至 row_size。

### 7.7 帧同步（滑动窗口）

CAM2 帧 = `magic(4) + W(2) + H(2) + W·H`。接收缓冲 $B$ 中查找：

$$\text{帧完整} \iff \exists\, i:\ B[i{:}i{+}4] = \text{"CAM2"} \ \land \
(B[i{+}4]{:}i{+}6) = (W, H) \ \land \ i+8+W{\cdot}H \le |B|$$

命中后丢弃前缀、保留尾部残余继续累积。**串口读法硬约束**：大块
`read(4096)` + 内部缓冲 + `find()` 滑窗；绝不做逐字节 `read(1)`
（否则 CDC 缓冲填满、板子 `Serial.write` 阻塞、诊断命令饿死）。

### 7.8 CFA 通道均值

逐通道统计（分离度/场景判断）：

$$\mu_c = \frac{1}{|\Omega_c|} \sum_{(y,x)\in \Omega_c} I(y,x), \qquad
\Omega_c = \{(y,x): c(y,x) = c\}$$

### 7.9 帧率与 USB 吞吐

CDC 全速 12 Mbps 物理上限，有效吞吐 ~660 KB/s：

$$FPS \approx \frac{660\ \text{KB/s}}{153{,}600\ \text{B/帧}} \approx 4.2$$

单缓冲使采集与发送串行——`frame_ready` 在发送完成后才清除，避免覆盖在发缓冲。
双缓冲乒乓不可行：$2 \times 153{,}600 = 300\ \text{KB} > 264\ \text{KB SRAM}$。

---

## 8. 验证体系（纯软件 + 硬件）

```
test/
├── test_pipeline_math.py   20 用例  纯软件（CI 无板）: RGB565 解码/BMP/端到端
├── test_bayer_math.py      11 用例  纯软件: RGGB 提取/双线性/最近邻/BMP
├── test_bayer_capture.py   14 用例  纯软件: CAM2 封装/滑窗/缝合/窗口编码/CFA 均值
├── test_hw_integration.py   4 用例  真机: 寄存器回读/CAM1 帧流/波形/帧率（RGB565 固件）
└── test_hw_bayer.py         4 用例  真机: 寄存器回读/CAM2 帧流/dup_even/缝合+BMP
```

- **固件模式探针**：两个硬件文件在 `setUpClass` 里用 `'R'` 读 COM7 自动互选。
  `test_hw_bayer.py`：COM7≠0x01 → 整类 skip；`test_hw_integration.py`：COM7=0x01 → 整类 skip。
- **类级 skip 不进入 Ran 计数**：`setUpClass` 抛 SkipTest 时，该类的 4 个方法
  被并入 1 条 skip 条目，不计入 `Ran N tests`。故 53 个方法定义，实测
  `Ran 49` = 45 纯软件 + 4 hw_bayer。
- **无板**：探针失败（无串口）→ 两个硬件类各整类 skip；纯软件 45 用例照常通过。
- **运行前提**：板上必须烧对应固件；`test_01` 断言 init 后**全窗**值
  （VSTART=0x03/VSTOP=0x7B）——若窗口残留 half 态（采集后停在 lower 窗口
  0x19=0x3F/0x1A=0x7B）会单测 skip，需重新上电/重烧。

实测（raw bayer 固件在板）：
- 重烧后全量：`Ran 49 tests` OK，skipped=1（仅 hw_integration 类级，正确）；
  `test_hw_bayer -v` **4/4 PASS**。
- 采集后（窗口残留 half）：`Ran 49`，skipped=2（多出 test_01 半窗 skip，重烧即恢复）。

---

## 9. 操作手册

### 9.1 构建与烧录

```bash
# 编译（RAW_BAYER: -DRAW_BAYER -DFRAME_W=640 -DFRAME_H=240）
pio run -e rpipico

# 烧录（MCP upload 被策略拦截时用 CLI；板子死机先按住 BOOTSEL 上电）
pio run -e rpipico -t upload --upload-port /dev/cu.usbmodemXXXX
```

### 9.2 采集（真机）

```bash
# 依赖 pyserial；纯函数层零依赖
python3 bayer_capture.py --port /dev/cu.usbmodemXXXX --out bayer_out --pairs 2 --bmp
```

- 首条命令必须是 `'T' upper`（init 后窗口为全窗）。
- 每对 = upper 640×240 + lower 640×240 + `stitch_halves` 640×480 raw
  + 可选去马赛克 BMP + 每对/总体的 CFA 均值 JSON。

### 9.3 测试

```bash
# 全套（无板自动 skip 硬件）
python3 -m unittest discover -s test -p "test_*.py"

# 只跑硬件（板已连）
python3 -m unittest test.test_hw_bayer -v
python3 -m unittest test.test_hw_integration -v
```

> ⚠️ 采集/测试会留下 half 窗口 → 再次跑 `test_01`（断言全窗）前需重烧。

### 9.4 离线去马赛克

```bash
python3 bayer_demosaic.py in.raw --width 640 --height 480 --pattern RGGB -o out.bmp
python3 bayer_demosaic.py in.raw --width 640 --height 240 --nearest -o out.bmp
```

---

## 10. 结果与证据

### 10.1 最终硬件验证（2026-08-14）

- 全量：`Ran 50 tests` OK（45 纯软件 + 5 hw_bayer），hw_integration 类
  整类 skip（板上为 raw bayer 固件，COM7=0x01 探针，预期 1 条类级 skip）。
- `test_hw_bayer -v`（shipped 固件）：**5/5 PASS**（test_01 寄存器回读、
  test_02 帧统计 nz>50% + >100 种取值、test_03 dup_even<0.9、test_04
  缝合+BMP、test_05 'C' PCLK 边沿计数）。

### 10.2 采集统计（bayer_out/stats.json，修复后实采）

| 区域 | R | G | B |
|---|---|---|---|
| 上半总 | 37.1 | 34.4 | 33.0 |
| 下半总 | 121.9 | 60.8 | 123.5 |
| pair0 上 | 18.4 | 54.9 | 10.2 |
| pair1 上 | 55.7 | 13.9 | 55.9 |
| pair0/pair1 下 | ≈122 / 61 / 124 | | |

下半帧高且稳定（R≈B≈2G，典型中性场景）；上半帧随场景内容变化 ——
与 RGGB 传感器在场景光照下的期望一致，无单通道崩溃。

### 10.3 关键修复证据链

1. `dup_even` 1.000 → 0.385（缺陷消除）；
2. 640 宽 colLag2=0.426 >> colLag1=0.146（Bayer 结构完整，假设 B 成立）；
3. 320 重塑无签名（假设 A 证伪，FRAME_W=640 保持）；
4. 跨帧相关更稳定（upper 0.29→0.66，lower 0.44→0.87）；
5. 8 组寄存器配置全无效（非寄存器问题）；'C' 直测 1280 边沿/行（shipped）
   vs 640 边沿/行（official，但仅 320 不同字节/行）——2 PCLK/byte 与配置无关。

### 10.4 提交记录（exp/raw-bayer）

| Commit | 内容 |
|---|---|
| `cda8080` | fix(bayer): every-2nd-PCLK sampling 消除水平 2x 重复（dup_even 1.0→0.385）+ 全部注释/文档核对为确认结论 |
| `ec632ff` | docs(plan): T7 commit 步骤标记完成 |

### 10.5 'C' 直接测量 + 第 8 组 A/B（2026-08-14 决定性验证）

**背景**：§6.3 判定性实验（'C' 命令 SM2 PIO 计数每 HREF 行 PCLK 上升沿）
完成，将 "1280 PCLK/行" 从推断变为**直接测量**；同时用官方 Table 2-2
Sheet 3 寄存器表（`-DRAW_BAYER_OFFICIAL_REGS`）做第 8 组 A/B，检验
2 PCLK/byte 是否与寄存器配置无关。

| 测量（4 行取一致） | shipped（当前表） | official（Table 2-2 Sheet 3） |
|---|---|---|
| 'C' 边沿/行 | **1280**（=0xFFFFFF00） | **640**（=0xFFFFFF80） |
| per-PCLK 采样 dup_even | —（shipped 用 every-2nd） | **1.000**（240/240 行） |
| 每行不同字节数 | 640 | **320**（唯一值 median 62，min 40 max 93） |
| 帧完整性 | 640×240 153600 B | 640×240 153600 B（仍完整） |

- **official 配置下 'C'=640 边沿/行与 datasheet Table 3-3 吻合**，但
  per-PCLK 采样后每行**只有 320 个不同字节**（DCWCTR=0x11 HDS by 2 +
  XSC/YSC 缩放使行内不同字节减半）→ 640 边沿只承载 320 个不同字节，
  每个字节仍保持 2 PCLK（colLag2=14.272 ≈ 2×colLag1=7.125 重复指纹）。
- **结论**：2 PCLK/byte 与寄存器配置**无关**（8 组配置全验证）；
  datasheet 的 640 PCLK/行时序仅适用于官方缩放配置，且该配置输出行
  也只有 320 个不同字节。**shipped 配置 + every-2nd-PCLK 采样是全分辨率
  （640 不同字节/行）唯一路径**，§6.5 修复保持正确。
- 配套：`test_05` 把 640/1280 边沿数断言进硬件测试（±3 同步竞态）；
  test_01 按 CLKRC 回读 0x01 自动选 official 期望表；`rpipico_official`
  env 中 test_03 **预期失败**（dup_even=1.000 ≥ 0.9，per-PCLK 下官方配置
  本来就重复——见 test/README.md）。

---

## 11. 附录

### 11.1 完整寄存器表（RAW_BAYER 回读 24 项，test_01 断言）

| 地址 | 名称 | 期望 | 关键位 |
|---|---|---|---|
| 0x0A | PID | 0x76 | OV7670 产品 ID |
| 0x0B | VER | 0x73 | 版本号 |
| 0x12 | COM7 | 0x01 | sensor raw（8-bit Bayer） |
| 0x40 | COM15 | 0xD0 | 全 0–255 范围 |
| 0x15 | COM10 | live | 实时状态 |
| 0x11 | CLKRC | 0x80 | fINT = XCLK/2（20.8 MHz PWM 构建） |
| 0x6B | DBLV | 0x0A | PLL |
| 0x1E | MVFP | 0x07 | 不翻转 |
| 0x13 | COM8 | live | AGC/AEC 状态 |
| 0x17 | HSTART | 0x11 | |
| 0x18 | HSTOP | 0x61 | |
| 0x19 | VSTART | 0x03 | 全窗 |
| 0x1A | VSTOP | 0x7B | 全窗 |
| 0x03 | VREF | 0x03 | 窗口低 4 位 |
| 0x32 | HREF | 0x80 | HSTART/HSTOP 高 6 位 |
| 0x70/0x71 | XSC/YSC | 0x00/0x00 | 缩放旁路（匹配 RGB565） |
| 0x0C | COM3 | 0x00 | 缩放/变焦旁路 |
| 0x3E | COM14 | 0x18 | bit4+bit3 打开 0x73 门；bits[2:0]=000 PCLK /1 |
| 0x72 | DCWCTR | 0x00 | 不下采样（默认 0x11 = HDS by 2，Table 6-2） |
| 0x73 | PCLK_DIV | 0x08 | bit[3]=1 旁路分频器（匹配 RGB565） |
| 0x74 | REG74 | 0x20 | 水平缩放比 1x（Table 6-1；匹配 RGB565） |
| 0x75 | REG75 | 0x0F | init 未写；reset 默认 |
| 0xA2 | PCLK_DELAY | 0x02 | Table 2-2 VGA raw 参考 |

### 11.2 相关文件

```
docs/RAW_BAYER_OPERATION_MATH.md   本文档
docs/superpowers/plans/2026-08-13-raw-bayer.md   完整执行计划（T1–T7）
src/camera.pio                     PIO 程序（每 2nd PCLK 采样）
src/ov7670.c/.h                    OV7670 驱动 + 窗口切换
src/main.cpp                       CAM2 协议 + 'T'/'R' 诊断
bayer_capture.py                   采集 CLI + 纯函数层
bayer_demosaic.py                  去马赛克 + BMP
test/                              纯软件 + 硬件测试
bayer_out/                         实采证据（raw + BMP + stats.json）
```

### 11.3 参考

- OV7670 datasheet §6.5（窗口寄存器）、Table 6-1/6-2/6-3（缩放与 PCLK/byte）。
- [docs/OV7670_RP2040_REFERENCE.md](OV7670_RP2040_REFERENCE.md) — RGB565 参考研究。
- RP2040 PIO 数据手册（`wait`/`in` 指令、绝对 GPIO wait 语义）。
