# OV7670 + RP2040 PIO — Research Reference

Extracted from 3 open-source repos (clones in `/tmp/ov_ref/`). Target: RGB565 QVGA (320x240), direct-parallel wiring (D0..D7 + HREF + VSYNC + PCLK), matching this project's pinout.

**Key finding: none of the reference repos defaults to RGB565 — all default to YUV.** For RGB565 you must use the `OV7670_rgb` format table + `OV7670_SIZE_DIV2`.

---

## 1. Reference repos & permalinks

| Repo | SHA (HEAD) | Approach |
|---|---|---|
| [tvlad1234/pico-ml-camera](https://github.com/tvlad1234/pico-ml-camera) | `216951ee732bfeb6f4a53e33ecebfd7c34289158` | Direct-parallel PIO (D0-D7+HREF+VSYNC+PCLK), Adafruit driver ported to C. **Best match for this project.** |
| [usedbytes/camera-pico-ov7670](https://github.com/usedbytes/camera-pico-ov7670) | `6c605e6b623a00994e19396363609ca27f75b231` | Frame-SM + 4 byte-shift SMs, IRQ-synced, patched pixel loops |
| [mxyxbb/rp2040_ov7670_usb_camera](https://github.com/mxyxbb/rp2040_ov7670_usb_camera) | `c1020302d3dfc00a8ebefed629d97360c3f12168` | Free-running parallel PIO + UVC USB, CSDN-style reg table |

### Init calls used by each repo (all YUV!)
- tvlad1234: `OV7670_begin(&cam, OV7670_COLOR_YUV, OV7670_SIZE_DIV4, 25)` — [ml_cam.c:189](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ml_cam.c#L189)
- usedbytes: `OV7670_begin(..., OV7670_COLOR_YUV, OV7670_SIZE_DIV8, 0.0)` — [camera.c:133](https://github.com/usedbytes/camera-pico-ov7670/blob/6c605e6b623a00994e19396363609ca27f75b231/camera.c#L133)
- mxyxbb: `ov2640_regs_write(config, OV7670_yuv); // OV7670_rgb` (RGB commented out) — [ov7670.c:43](https://github.com/mxyxbb/rp2040_ov7670_usb_camera/blob/c1020302d3dfc00a8ebefed629d97360c3f12168/src/ov7670/ov7670.c#L43)

---

## 2. Register init tables (RGB565 path)

### Format table — write FIRST (Adafruit, [ov7670.c:45-58](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/ov7670.c#L45-L58))

```c
// RGB565, full 0-255 output range
static const OV7670_command OV7670_rgb[] = {
    {OV7670_REG_COM7,   OV7670_COM7_RGB},                    // 0x12 = 0x04  (RGB format)
    {OV7670_REG_RGB444, 0},                                  // 0x8C = 0x00  (disable RGB444)
    {OV7670_REG_COM15,  OV7670_COM15_RGB565 | OV7670_COM15_R00FF}, // 0x40 = 0x10|0xC0 = 0xD0
    {0xFF, 0xFF}};                                           // terminator
```

Verified defines ([ov7670.h:109-118](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/ov7670.h#L109-L118), [190-198](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/ov7670.h#L190-L198)):
`COM7=0x12`, `COM7_RGB=0x04`, `COM7_YUV=0x00`; `COM15=0x40`, `COM15_RGB565=0x10`, `COM15_R00FF=0xC0` → **COM15 = 0xD0** (matches mxyxbb's `{0x40, 0xd0}` [ov7670_init.h:24](https://github.com/mxyxbb/rp2040_ov7670_usb_camera/blob/c1020302d3dfc00a8ebefed629d97360c3f12168/src/ov7670/ov7670_init.h#L24)).

### Main init table ([ov7670.c:59-161](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/ov7670.c#L59-L161))

```c
static const OV7670_command OV7670_init[] = {
    {OV7670_REG_TSLB, OV7670_TSLB_YLAST},    // 0x3A = 0x04  no auto window
    {OV7670_REG_COM10, OV7670_COM10_VS_NEG}, // 0x15 = 0x02  -VSYNC
    {OV7670_REG_SLOP, 0x20},                 // 0x7A
    {OV7670_REG_GAM_BASE, 0x1C},             // 0x7B..0x89 gamma curve (15 regs)
    {OV7670_REG_GAM_BASE + 1, 0x28},
    {OV7670_REG_GAM_BASE + 2, 0x3C},
    {OV7670_REG_GAM_BASE + 3, 0x55},
    {OV7670_REG_GAM_BASE + 4, 0x68},
    {OV7670_REG_GAM_BASE + 5, 0x76},
    {OV7670_REG_GAM_BASE + 6, 0x80},
    {OV7670_REG_GAM_BASE + 7, 0x88},
    {OV7670_REG_GAM_BASE + 8, 0x8F},
    {OV7670_REG_GAM_BASE + 9, 0x96},
    {OV7670_REG_GAM_BASE + 10, 0xA3},
    {OV7670_REG_GAM_BASE + 11, 0xAF},
    {OV7670_REG_GAM_BASE + 12, 0xC4},
    {OV7670_REG_GAM_BASE + 13, 0xD7},
    {OV7670_REG_GAM_BASE + 14, 0xE8},
    {OV7670_REG_COM8, 0x80|0x40|0x20},       // 0x13 fast AEC/banding (AGC/AEC on later)
    {OV7670_REG_GAIN, 0x00},                 // 0x00
    {OV7670_COM2_SSLEEP, 0x00},              // 0x09
    {OV7670_REG_COM4, 0x00},                 // 0x0D
    {OV7670_REG_COM9, 0x20},                 // 0x14 max AGC
    {OV7670_REG_BD50MAX, 0x05},              // 0xA5
    {OV7670_REG_BD60MAX, 0x07},              // 0xAB
    {OV7670_REG_AEW, 0x75},                  // 0x24
    {OV7670_REG_AEB, 0x63},                  // 0x25
    {OV7670_REG_VPT, 0xA5},                  // 0x26
    {OV7670_REG_HAECC1, 0x78},               // 0x9F histogram AEC/AGC
    {OV7670_REG_HAECC2, 0x68},               // 0xA0
    {0xA1, 0x03},
    {OV7670_REG_HAECC3, 0xDF},               // 0xA6
    {OV7670_REG_HAECC4, 0xDF},               // 0xA7
    {OV7670_REG_HAECC5, 0xF0},               // 0xA8
    {OV7670_REG_HAECC6, 0x90},               // 0xA9
    {OV7670_REG_HAECC7, 0x94},               // 0xAA
    {OV7670_REG_COM8, 0x80|0x40|0x20|0x04|0x01}, // enable AGC+AEC now
    {OV7670_REG_COM5, 0x61},                 // 0x0E
    {OV7670_REG_COM6, 0x4B},                 // 0x0F
    {0x16, 0x02},
    {OV7670_REG_MVFP, 0x07},                 // 0x1E mirror/vflip
    {OV7670_REG_ADCCTR1, 0x02},              // 0x21
    {OV7670_REG_ADCCTR2, 0x91},              // 0x22
    {0x29, 0x07},
    {OV7670_REG_CHLF, 0x0B},                 // 0x33
    {0x35, 0x0B},
    {OV7670_REG_ADC, 0x1D},                  // 0x37
    {OV7670_REG_ACOM, 0x71},                 // 0x38
    {OV7670_REG_OFON, 0x2A},                 // 0x39
    {OV7670_REG_COM12, 0x78},                // 0x3C
    {0x4D, 0x40}, {0x4E, 0x20},
    {OV7670_REG_GFIX, 0x5D},                 // 0x69
    {OV7670_REG_REG74, 0x19},                // 0x74
    {0x8D, 0x4F}, {0x8E, 0x00}, {0x8F, 0x00},
    {0x90, 0x00}, {0x91, 0x00},
    {OV7670_REG_DM_LNL, 0x00},               // 0x92
    {0x96, 0x00}, {0x9A, 0x80}, {0xB0, 0x84},
    {OV7670_REG_ABLC1, 0x0C},                // 0xB1
    {0xB2, 0x0E},
    {OV7670_REG_THL_ST, 0x82},               // 0xB3
    {0xB8, 0x0A},
    {OV7670_REG_AWBC1, 0x14},                // 0x43
    {OV7670_REG_AWBC2, 0xF0},                // 0x44
    {OV7670_REG_AWBC3, 0x34},                // 0x45
    {OV7670_REG_AWBC4, 0x58},                // 0x46
    {OV7670_REG_AWBC5, 0x28},                // 0x47
    {OV7670_REG_AWBC6, 0x3A},                // 0x48
    {0x59, 0x88}, {0x5A, 0x88}, {0x5B, 0x44},
    {0x5C, 0x67}, {0x5D, 0x49}, {0x5E, 0x0E},
    {OV7670_REG_LCC3, 0x04},                 // 0x64 lens correction
    {OV7670_REG_LCC4, 0x20},                 // 0x65
    {OV7670_REG_LCC5, 0x05},                 // 0x66
    {OV7670_REG_LCC6, 0x04},                 // 0x94
    {OV7670_REG_LCC7, 0x08},                 // 0x95
    {OV7670_REG_AWBCTR3, 0x0A},              // 0x6C
    {OV7670_REG_AWBCTR2, 0x55},              // 0x6D
    {OV7670_REG_MTX1, 0x80},                 // 0x4F color matrix
    {OV7670_REG_MTX2, 0x80},                 // 0x50
    {OV7670_REG_MTX3, 0x00},                 // 0x51
    {OV7670_REG_MTX4, 0x22},                 // 0x52
    {OV7670_REG_MTX5, 0x5E},                 // 0x53
    {OV7670_REG_MTX6, 0x80},                 // 0x54
    {OV7670_REG_AWBCTR1, 0x11},              // 0x6E
    {OV7670_REG_AWBCTR0, 0x9F},              // 0x6F simple AWB
    {OV7670_REG_BRIGHT, 0x00},               // 0x55
    {OV7670_REG_CONTRAS, 0x40},              // 0x56
    {OV7670_REG_CONTRAS_CENTER, 0x80},       // 0x57
    {OV7670_REG_LAST + 1, 0x00},             // end-of-data marker
};
```

### QVGA windowing — `OV7670_set_size(DIV2)` ([ov7670.c:350-370](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/ov7670.c#L350-L370), [297-348](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/ov7670.c#L297-L348))

QVGA = `{vstart=10, hstart=174, edge_offset=4, pclk_delay=2}` → writes: `COM3=0x04` (DCWEN), `COM14=0x1A`, `SCALING_DCWCTR=0x22`, `SCALING_PCLK_DIV=0xF2`, `SCALING_XSC/YSC=0x20`, then H/V window regs (`HSTART 0x17`, `HSTOP 0x18`, `HREF 0x32`, `VSTART 0x19`, `VSTOP 0x1A`, `VREF`). mxyxbb equivalent: `{0x17,0x16},{0x18,0x04},{0x19,0x02},{0x1a,0x7b},{0x32,0x80},{0x03,0x06}` ([ov7670_init.h:144-149](https://github.com/mxyxbb/rp2040_ov7670_usb_camera/blob/c1020302d3dfc00a8ebefed629d97360c3f12168/src/ov7670/ov7670_init.h#L144-L149)).

---

## 3. PIO program (direct-parallel, best match)

tvlad1234's frame program — [camera_frame.pio](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/camera_frame.pio):

```pio
.define PUBLIC PIN_OFFS_PCLK    8
.define PUBLIC PIN_OFFS_HREF     9
.define PUBLIC PIN_OFFS_VSYNC    10

.program camera_frame
.wrap_target
pull                          ; Pull number of lines from FIFO
out Y, 32                     ; Store number of lines in Y
pull                          ; Pull bytes-per-line from FIFO into OSR

wait 0 pin PIN_OFFS_VSYNC     ; Wait for VSYNC rising edge
wait 1 pin PIN_OFFS_VSYNC

loop_line:                    ; For each line in frame
mov X, OSR                    ; Reload X with bytes-per-line
wait 0 pin PIN_OFFS_HREF      ; Wait HSYNC rising edge
wait 1 pin PIN_OFFS_HREF

loop_byte:                    ; For each byte in line
wait 1 pin PIN_OFFS_PCLK      ; Wait for PCLK rising edge
in PINS 8                     ; Shift in 1 byte
wait 0 pin PIN_OFFS_PCLK      ; Wait for PCLK falling edge
jmp x-- loop_byte             ; Next byte

jmp y-- loop_line             ; Next line
irq wait 0                    ; Throw interrupt, wait for ack
.wrap                         ; Next frame
```

Setup: `in_shift = left, autopush, 8 bits` → each byte auto-pushed to RX FIFO; all 11 pins inputs (`set_consecutive_pindirs(..., 11, false)`); DMA DREQ = `pio_get_dreq(pio, sm, false)`.

**Alternatives:**
- mxyxbb free-running variant ([image.pio](https://github.com/mxyxbb/rp2040_ov7670_usb_camera/blob/c1020302d3dfc00a8ebefed629d97360c3f12168/src/ov7670/image.pio)): `wait 0 pin 9 / wait 1 pin 9` HREF gate, CPU syncs VSYNC via GPIO, DMA counts fixed 153,600-byte frame.
- usedbytes frame-SM + 4 byte-SMs with IRQ handshake ([camera.pio:50-80](https://github.com/usedbytes/camera-pico-ov7670/blob/6c605e6b623a00994e19396363609ca27f75b231/camera.pio#L50-L80), RGB565 pixel loop [133-141](https://github.com/usedbytes/camera-pico-ov7670/blob/6c605e6b623a00994e19396363609ca27f75b231/camera.pio#L133-L141)).

---

## 4. DMA / capture flow

**tvlad1234** ([rp2040.c:14-53](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/arch/rp2040.c#L14-L53)) — cleanest for this project:
1. `pio_sm_put_blocking(pio, sm, lines-1)` then `pio_sm_put_blocking(pio, sm, 2*cols-1)` — PIO starts, syncs VSYNC/HREF itself
2. DMA: read `&pio->rxf[sm]` (no increment), write `buf` (increment), `DMA_SIZE_8`, count = `lines * 2 * cols`, DREQ = PIO RX
3. `dma_channel_start()` → wait `frame_ready` flag (set by PIO `irq wait 0` handler) → `dma_channel_wait_for_finish_blocking()`

**mxyxbb** ([ov7670.c:57-84](https://github.com/mxyxbb/rp2040_ov7670_usb_camera/blob/c1020302d3dfc00a8ebefed629d97360c3f12168/src/ov7670/ov7670.c#L57-L84)): CPU waits VSYNC edge on GPIO, then `dma_channel_start()` → blocking finish. Frame size `IMG_BUF_SIZE = 320*240*2 = 153600` ([main.c](https://github.com/mxyxbb/rp2040_ov7670_usb_camera/blob/c1020302d3dfc00a8ebefed629d97360c3f12168/src/main.c)).

**usedbytes** ([camera.c:309-353](https://github.com/usedbytes/camera-pico-ov7670/blob/6c605e6b623a00994e19396363609ca27f75b231/camera.c#L309-L353)): `dma_channel_configure(..., buf->data[i], &pio->rxf[i+1], transfers, true)`, then `camera_pio_trigger_frame(pio, cols, rows)` pushes line/byte counts via FIFO.

---

## 5. Comparison with this project's `src/main.cpp`

Current main.cpp uses a **6-instruction free-running PIO** (hand-encoded `0x2080..0x0000`, autopush @32-bit, DMA 32-bit `IMG_SIZE/4` words) with a **custom short register table** (24 regs). Gaps vs reference:

| Aspect | main.cpp (current) | Reference (tvlad1234) |
|---|---|---|
| PIO | 6 instr, HREF+PCLK only, no VSYNC, no line/byte counting — frame boundary is DMA count only | 22 instr, VSYNC+HREF+PCLK gated, line/byte counted, `irq wait 0` frame-done handshake |
| Init table | short custom subset, several values deviate from Adafruit (e.g. COM10=0x6B/0x0A, COM12=0x3D/0xC1, window 0x17=0x13...) | full Adafruit `OV7670_init` (~100 regs) + `OV7670_rgb` |
| Format | COM15=0x10 only (RGB565 but R00FF bit missing → 0-247 range) | COM15=0xD0 (RGB565|R00FF → full 0-255) |
| DMA | 32-bit words from RX FIFO (autopush 32) | 8-bit bytes from RX FIFO (autopush 8) |

**Bottom line**: to get RGB565 QVGA with correct frame boundaries, swap in tvlad1234's PIO program + `OV7670_rgb` + `OV7670_init` + `set_size(DIV2)` window regs; keep the existing VSYNC GPIO sync or switch to PIO `irq wait 0`.

---

## 6. Config notes

- tvlad1234 XCLK: 12.5 MHz (`OV7670_XCLK_HZ 12500000` in [arch_rp2040.h](https://github.com/tvlad1234/pico-ml-camera/blob/216951ee732bfeb6f4a53e33ecebfd7c34289158/ov7670/arch/arch_rp2040.h)) — this project uses ~24.2 MHz PWM (main.cpp `startXclk`, wrap=1, div 5.5) — both are within spec (OV7670 max 24 MHz).
- mxyxbb XCLK: PWM wrap=4 (~20.83 MHz), SCCB i2c 10 kHz ([ov7670.c:14-55](https://github.com/mxyxbb/rp2040_ov7670_usb_camera/blob/c1020302d3dfc00a8ebefed629d97360c3f12168/src/ov7670/ov7670.c#L14-L55)) — matches this project's 10 kHz I2C.
- Board: `vccgnd_yd_rp2040` (platformio.ini) — arduino-pico core (earlephilhower), exposes pico-sdk PIO/DMA + Wire pin remap. Fine for the reference PIO/DMA code.

*Clones kept at `/tmp/ov_ref/` for further diffs.*
