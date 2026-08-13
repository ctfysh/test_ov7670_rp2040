# Raw Bayer 验证设计 — VGA 半帧拼接

- **日期**: 2026-08-13
- **分支**: `exp/raw-bayer`（从 main 切出）
- **状态**: 已获用户批准（设计呈现后确认"先执行"）

## 1. 背景与目标

OV7670 采集管线已在 main 分支完成 QVGA RGB565 全链路验证（数学 20 + 真机 4，全绿）。
本任务验证 **Raw Bayer 输出模式**（数据手册唯一支持 Raw Bayer 的分辨率为 VGA 640×480），
交付三件产物：

1. 固件采集 VGA 640×480 8-bit Raw Bayer（BGGR）
2. Bayer 数学验证（CFA 排列 + 去马赛克插值）
3. 真机统计 + 去马赛克出图

### 硬件约束（决定性因素）

**RP2040 264 KB SRAM 放不下 VGA Bayer 整帧**：640×480×1 B = 307,200 B > 270,336 B。
采集速率（PCLK ~10 MHz ≈ 10 MB/s）比 USB CDC 发送（~660 KB/s）快 ~15 倍，**必须整帧
缓冲才能流式发送**，因此"边采边发"不可行；16 MB QSPI Flash 可中转但每帧需擦 75 个
sector（~300 ms）+ 写 1200 页（~600 ms），DMA 又不能直写 XIP，得不偿失。

**采用方案：VGA 窗口半帧拼接**（用户选定）。

## 2. 数据手册权威事实（来源 docs/ov7670_ig.txt）

| 事实 | 内容 | 出处 |
|---|---|---|
| Raw Bayer 判定 | COM7[2]=0, COM7[0]=1, COM15[5]=x, COM15[4]=0；8-bit R/G/B 无 ISP | Table 2-1 (p.8) |
| 分辨率 | Raw Bayer **仅 VGA** 640×480；Processed Bayer 才支持 QVGA | §2.1 (p.8) |
| VGA 缩放组 | CLKRC=0x01, COM7=0x00, COM3=0x00, COM14=0x00, SCALING_XSC=0x3A, SCALING_YSC=0x35, SCALING_DCWCTR=0x11, SCALING_PCLK_DIV=0xF0, SCALING_PCLK_DELAY=0x02 | Table 2-2 (p.9) |
| 窗口化语义 | 窗口化不改变帧率/数据率；HREF 跟随编程区域；默认窗口 640×480 | §6.5 (p.41) |
| 窗口寄存器 | VSTRT=(0x19<<2)\|VREF[1:0]；VSTOP=(0x1A<<2)\|VREF[3:2]；HSTART=(0x17<<3)\|HREF[2:0]；HSTOP=(0x18<<3)\|HREF[5:3] | 寄存器表 (p.48-53) |
| 窗口调整前提 | 必须先写 TSLB[0]=0 (0x3A) | §2.1 (p.8) |
| 默认全窗口 | VSTRT=15, VSTOP=492（≈480 行）；HSTART=136, HSTOP=776（≈640 列） | 默认值反推 (p.48-49) |

**Raw Bayer 配置 = Table 2-2 VGA 行 + COM7 改 0x01**（bit0=1=Raw, bit2=0, bit4=0=VGA）。
当前固件 COM15=0xD0 的 bit[5:4]=x0 已兼容 Raw Bayer 判定。

## 3. 架构：VGA 半帧拼接

```
OV7670 VGA 640x480 RAW (BGGR)
        │ 窗口寄存器切行
        ├─ half=0: 上半 240 行 (VSTRT≈15..VSTOP≈252)  → 640×240 = 153,600 B
        └─ half=1: 下半 240 行 (VSTRT≈252..VSTOP≈492) → 640×240 = 153,600 B
        │
        ▼ CAM2 协议 (8-bit)
RP2040 USB CDC → 主机 bayer_capture.py
        │ 抓上半 N 帧 + 'T' 切窗口 + 抓下半 N 帧
        ▼
拼接 640×480 .raw → bayer_demosaic.py (BGGR 双线性) → BMP
```

- 每半帧 153,600 B，与 main 分支 QVGA RGB565 帧尺寸相同 → DMA/缓冲/内存架构零改动
- 帧率：VGA 全帧 ~13 fps（20.83 MHz XCLK），半帧窗口不改变帧率；CDC 发送 153,600 B
  ≈ 0.23 s/帧 ≈ ~4.3 fps 为实际上限
- 窗口切换通过 SCCB 改写 0x19/0x1A/0x03（3 个寄存器），帧边界稳定后生效

## 4. 固件改动

### src/ov7670.c / ov7670.h

- 新增 `ov7670_init_raw_bayer()`：写入 Table 2-2 VGA 缩放组 + COM7=0x01 + COM15 保持
  （bit[5:4]=x0 兼容）+ TSLB[0]=0 + VGA 默认窗口（显式复位，防止继承 QVGA 表窗口值）
- 新增 `ov7670_set_bayer_window(uint8_t half)`：
  - half=0（上半）：VSTRT≈15, VSTOP≈252
  - half=1（下半）：VSTRT≈252, VSTOP≈492
  - 按 10-bit 公式写 0x19/0x1A/0x03（高位拆入 VREF）
- 窗口边界值在真机验证阶段用 'R' 回读 + 帧统计校准（边界可能有 1~2 行偏移）

### src/main.cpp

- 新增命令 `'T'` + 1 字节参数（0=上半, 1=下半）：SCCB 切窗口，回复 `DBG1+0xF9+half` 确认
- 新增帧协议 `CAM2`：`"CAM2"`(4B) + W(2BE) + H(2BE) + W×H 字节 8-bit Bayer 半帧
- 保留 CAM1/DBG1 全部诊断（'W'/'S'/'R'/'B' 不动）；'R' 回读期望值注释更新
  （COM7 期望 0x01 而非 0x14）

### platformio.ini

- `-DFRAME_W=640 -DFRAME_H=240`（半帧 153,600 B；`FRAME_BYTES = FRAME_W*FRAME_H` 需在
  main.cpp 区分 CAM1 的 ×2 与 CAM2 的 ×1——实现时以 `#ifdef RAW_BAYER` 或协议分支处理）

## 5. 主机端工具（新文件，仓库根）

### bayer_capture.py

- CAM2 同步：大块 read(4096) + 内部缓冲 + find() 滑窗（沿用 main 分支串口读法纪律）
- 流程：sync → 抓上半 N 帧（默认 5）→ 发 'T'+1 → 等待 2 帧（寄存器生效延迟）→
  抓下半 N 帧 → 拼接 640×480 → 存 `.raw` + 统计 JSON（帧尺寸/magic/帧率/半帧字节数）
- 命令行：`--half-frames N --port ... --out dir`；拒绝 'B' 命令（BOOTSEL 防护）

### bayer_demosaic.py

- BGGR 双线性插值 → RGB → BMP（行序 bottom-up，沿 capture.py 惯例）
- 命令行可调 CFA 排列（`--pattern BGGR|GRBG|RGGB|GBRG`）用于真机比对
- 边缘像素用最近邻兜底（插值窗口越界）

## 6. 数学验证 test/test_bayer_math.py（纯软件，风格对齐现有 20 项）

1. **BGGR CFA 恒等**：坐标→颜色映射（偶行偶列=R, 偶行奇列=G, 奇行偶列=G, 奇行奇列=B）
2. **双线性去马赛克数学**：已知色块输入 → 精确期望输出（G 先插值、R/B 用 G 校正后插值）
3. **最近邻去马赛克**：直接取最近 CFA 同色
4. **半帧拼接**：上 240 行 + 下 240 行 → 640×480 行列对齐无缝隙（边界容差 1~2 行）
5. **CAM2 协议解析**：magic 校验、W/H BE 解析、载荷长度校验、噪声字节免疫（find 滑窗）

## 7. 真机验证 test/test_hw_bayer.py

1. build + CLI upload（MCP upload 被策略拦截 → `pio run -t upload`）→ FW 横幅 + 'R' 回读
   确认 COM7=0x01 / 窗口 / CLKRC 生效
2. CAM2 帧统计：magic/尺寸/帧率（预期 153,600 B @ ~4 fps CDC 上限）
3. **Bayer 特征验证**：稳定场景抓帧，统计 2×2 CFA 单元各位置均值——Bayer 模式下 R/B 位置
   均值明显分离（偏色），与 RGB565 插值帧对比
4. 半帧拼接 → 去马赛克 → BMP 存档 → 目检

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| QVGA 表已改窗口寄存器（0x17/18/19/1A）→ 切 RAW 必须复位 VGA 窗口，否则画面错乱 | 新 init 显式写 VGA 默认窗口 + 'R' 回读验证 |
| Table 2-2 假设 XCLK=24 MHz，本板 20.83 MHz → PCLK/帧率关系可能变 | CLKRC 决策留待真机回读；'R' 回读 CLKRC |
| 切窗口后寄存器生效有延迟 → 抓帧抢跑 | 'T' 后 sleep 2 帧（~0.5 s）再抓 |
| 上下边界 1~2 行偏移/重叠 | 拼接容差 + 统计验证 |
| CAM1/CAM2 帧字节数不同（×2 vs ×1） | 固件按协议分支计算载荷；测试分别校验 |

## 9. 文件清单

```
src/ov7670.c, ov7670.h      # RAW init + 窗口切换
src/main.cpp                # CAM2 协议 + 'T' 命令
platformio.ini              # FRAME_W=640 H=240
bayer_capture.py            # 新：半帧抓取 + 拼接 + 统计
bayer_demosaic.py           # 新：BGGR 去马赛克 → BMP
test/test_bayer_math.py     # 新：纯软件数学验证
test/test_hw_bayer.py       # 新：真机验证
test/README.md, README.md   # 更新
```

## 10. 验证顺序

1. spec 审阅（用户）→ writing-plans 出实施计划
2. 在 exp/raw-bayer worktree 实施固件改动
3. `python3 -m unittest discover -s test -p "test_*.py"`（数学项先行全绿）
4. build + CLI upload + 'R' 回读 + CAM2 统计
5. 半帧拼接 → 去马赛克 → BMP 存档 → 目检 → 交付汇报
