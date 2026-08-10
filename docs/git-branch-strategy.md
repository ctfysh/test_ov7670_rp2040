# Git 分支方案:main ↔ impl/uvc 并行开发

> 状态:设计定稿 v5(2026-08-10,已合入四轮外部审计意见)。本文档为分支策略参考,执行时遵循其中命令,改动前先更新本文档。
>
> 变更记录:
> - v1: 初始方案(impl/* 命名、worktree 并行、同步规则、执行清单)
> - v2: 合入第一轮审计(前置 checkout、README 不合并+PR 关闭声明、banner 代码、macOS 端口示例、worktree prune、单横线命名、2 条预防针)
> - v3: 合入第二轮审计(前置检查、README 描述精确化、banner Build 行、完成态确认)
> - v4: 合入第三轮审计(跨平台端口示例、防御措辞、VS Code 后果、变更记录)
> - v5: 合入第四轮审计(前置检查注释语气、串口锁描述化、完成态注释、适用范围)
>
> 适用范围:
> - 单人维护(多人协作的差异见第 7 节假设)
> - RP2040 + PlatformIO + TinyUSB / CDC 双实现
> - 两个长期并行维护的实现分支(非一次性实验)

## 1. 目标模型(命名规范)

```
main          # 参考实现: PIO+DMA 采集, CDC "CAM1" 协议 (live_view.py / capture.py)
impl/uvc      # 替代实现: 原生 USB UVC 摄像头 (live_view_uvc.py) — 长期存在,永不 merge 进 main
exp/*         # 实验原型,随时可删
opt/*         # 性能/SRAM 优化(归并回 main 或某 impl)
fix/*         # 短期修复(合并到受影响的实现分支)
cleanup/*     # 重构、非功能变更
port/*        # 跨板移植(预留)
release/*     # 发布分支(需要时启用)
```

**核心变更**:`feat/ov7670-uvc` → `impl/uvc`。理由:`feat/` 语义是"做完就合并";`impl/` 语义是"功能等价、实现不同的替代实现"——本项目场景是后者:永不合并、长期开发。

## 2. 并行开发机制:git worktree

一个工作目录只能 checkout 一个分支,并行开发两个长期分支需要两个目录:

```
~/.../projects/
 ├── test_ov7670_rp2040/       ← 现有目录,固定给 impl/uvc(UVC 活跃线)
 └── test_ov7670_rp2040-main/  ← 新 worktree,固定给 main
```

- 两个窗口各改各的、各推各的远端,零切换零 stash。
- 选 sibling 目录而非仓库内 `.worktrees/`:PIO/IDE 平级可见、免改 .gitignore。
- **worktree 目录命名统一单横线、带仓库前缀**(一眼可辨同仓库):现有目录(无后缀,= impl/uvc)、`test_ov7670_rp2040-main`(main),将来另开则 `test_ov7670_rp2040-uvc` 等。

## 3. 分支间同步规则

| 改动类型 | 落点 | 传播 |
|---|---|---|
| 共享核心修复(ov7670.c 时序、I2C、RGB565 字节序) | main | cherry-pick → impl/uvc |
| UVC 独有(uvc 描述符、live_view_uvc.py、USE_TINYUSB env) | 只留 impl/uvc | 不回传 |
| UVC 中发现并修复的共享 bug | impl/uvc | cherry-pick ← main |
| README 硬件文档、接线图 | 各自维护 | docs 同步 commit |

**铁律**:

- `impl/uvc` **永不** merge 进 `main`;`main` 保持纯参考实现。
- 两分支的 commit 必须各自 `pio run` 通过(impl/uvc 走 UVC env,main 走 CDC env)。

## 4. 已知预期(预防针)

```
1. GitHub UI 永久显示 impl/uvc 相对 main "N ahead"(定稿时实测 ahead_by=3、diff=7 文件);
   纯拓扑比较,≠ 冲突、≠ 待合并,永不归零;main 前进时 impl/uvc 显示 "behind" 同样正常;
   PR impl/uvc→main 的 diff 永远大 —— 设计使然,靠 README 声明对抗误解。
2. 双 worktree 易"忘记自己在哪"(尤其烧录/调试)——
   缓解: 固件 boot banner 自报身份(#ifdef USE_TINYUSB)+ 一板一目录(显式 upload_port)
         + 目录名带分支 + 烧录前 git worktree list;
   PlatformIO MCP 的串口访问是全局锁定的,两个 worktree 无法同时对同一块板
   执行 upload / monitor(事实描述,非本方案定下的规则)。
3. build dir 隔离 —— sibling worktree 默认已隔离(.pio 各目录;两分支 platformio.ini
   均未自定义 build_dir,已实测);禁令: 不设绝对/公共 build_dir;
   ~/.platformio 共享安全,仅首次并发构建可能自愈性竞争。
4. 双开 VS Code:每个窗口必须打开自己的 worktree 目录;.vscode/ 已 gitignore,天然独立;
   勿把两个目录塞进同一多根工程 —— 否则任务名/构建目标同名难分,点错分支,
   回到"忘记自己在哪"的老问题。
5. 跨 worktree checkout:main 已在另一 worktree 检出时,本地 `git checkout main` 会报
   `already checked out` —— 这是 Git 保护机制,不是错误;要让某 worktree 切到 main,
   须先 `git worktree remove` 另一处。
```

## 5. 执行清单(未执行)

```bash
# 前置检查:确认当前就在待更名的分支上
#   (双参 git branch -m 语法上不依赖 HEAD,此处仅为人工确认步骤,
#    避免在错误上下文中误操作)
git rev-parse --abbrev-ref HEAD   # 必须为 feat/ov7670-uvc(更名前),否则终止执行

# ① 更名 + 上游设置(push -u 一步完成,避免 branch -u 对 HEAD 的隐式依赖;
#    显式 checkout 防御"执行时不在该分支"的极端情况)
git branch -m feat/ov7670-uvc impl/uvc
git checkout impl/uvc
git push -u origin impl/uvc
git push origin :feat/ov7670-uvc
git fetch --prune

# ② 开 worktree
git worktree add ../test_ov7670_rp2040-main main
git worktree list   # 立即确认:应看到两个 worktree

# ③ README 补 Branch Strategy 表 + impl/* 不合并声明 + PR 关闭声明(见下)

# ④ 固件 boot banner(放在 src/main.cpp setup() 内、Serial.begin() 之后;
#    两分支同一份代码,零新宏):
#   Serial.println();
#   #ifdef USE_TINYUSB
#     Serial.println("FW: impl/uvc (TinyUSB UVC)");
#   #else
#     Serial.println("FW: main (PIO+DMA CDC)");
#   #endif
#   Serial.print("Build: "); Serial.print(__DATE__); Serial.print(" "); Serial.println(__TIME__);
#   用 print/println 而非 printf,各 core 零依赖;Build 行 = 编译日期,Phase 2 的
#   extra_scripts git hash 注入可无缝替换它(PlatformIO ini 顶层不支持 $(shell ...)
#   展开,git hash 注入走 extra_scripts Python 而非 ini 变量,勿用)。

# ⑤ 可选(一板一目录时): platformio.ini 显式 upload_port
#    示例:
#      macOS:   upload_port = /dev/cu.usbmodemXXXX
#      Linux:   upload_port = /dev/ttyACM0
#      Windows: upload_port = COM3
#    确认方法:
#      macOS/Linux: ls /dev/cu.* 或 ls /dev/ttyACM*(每板端口固定,不热插拔基本不变)
#      Windows:     Device Manager → Ports (COM & LPT)
#    注意: upload_port 会被 git 跟踪 —— 单人项目可接受;将来共享仓库改 CLI --upload-port。

# ⑥ 清理时(remove 已清理管理文件;prune 仅手工删目录/异常后才有意义,无害)
git worktree remove ../test_ov7670_rp2040-main
git worktree prune

# ⑦ 完成态确认(最终自检)
git worktree list
```

### README Branch Strategy 表(③ 的内容,可直接粘贴)

```markdown
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
```

## 6. 后续可选优化(Phase 2,与分支方案解耦)

把 `src/main.cpp` 的采集逻辑拆成 `src/capture/capture_dma.c` / `capture_uvc.c` + 宏选择 →
两分支 diff 收敛到只剩实现文件,cherry-pick 近乎零冲突,将来可合并成单一可配置分支。
banner 升级为 git commit hash 自动注入(extra_scripts)同属 Phase 2。

## 7. 假设

- 单人项目,无协作者。
- 两分支均长期维护,不是"UVC 做完就删"。
- UVC 为当前活跃线(现有目录留在 impl/uvc)。
