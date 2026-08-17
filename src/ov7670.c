// OV7670 no-FIFO driver for YD-RP2040
// - SCCB via hardware I2C0 on SIOC=GP21 / SIOD=GP20, 4.7k pull-ups
// - QVGA 320x240 RGB565 output
// - XCLK = 20.8 MHz hardware PWM (add -DXCLK_PWM) or 8 MHz PIO (default)
//
// Register/format strategy:
//   1. Soft reset (COM7 = 0x80)
//   2. CLKRC/DBLV for 8 MHz XCLK (PIO build only; PWM build keeps table)
//   3. Known-good full init table (CSDN, tuned for 24 MHz XCLK, QVGA config)
//   4. Override format to RGB565 + QVGA (window/scaling regs already QVGA
//      in the table; only clock-sensitive rows are re-applied)

#include "ov7670.h"

#include <stddef.h>
#include <stdint.h>

#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "hardware/structs/iobank0.h"
#include "pico/time.h"

// ---------------------------------------------------------------------------
// SCCB over hardware I2C (I2C-compatible, 4.7k external pull-ups assumed)
// ---------------------------------------------------------------------------

#define SCCB_SCL OV7670_PIN_SIOC
#define SCCB_SDA OV7670_PIN_SIOD

// Hardware I2C0: GP20=SDA, GP21=SCL are the I2C0 pin pair. The OV7670 SCCB
// bus is I2C-compatible; the reference implementation drives it with the
// hardware peripheral (10 kHz) rather than bit-banging.
#define SCCB_I2C i2c0

static int sccb_write_bytes(const uint8_t *data, size_t len) {
  return i2c_write_blocking(SCCB_I2C, OV7670_I2C_ADDR, data, len, false) == (int)len
             ? 0
             : -1;
}

static int sccb_read_bytes(uint8_t *data, size_t len) {
  return i2c_read_blocking(SCCB_I2C, OV7670_I2C_ADDR, data, len, false) == (int)len
             ? 0
             : -1;
}

int ov7670_write_reg(uint8_t reg, uint8_t value) {
  uint8_t data[2] = {reg, value};
  return sccb_write_bytes(data, 2);
}

int ov7670_read_reg(uint8_t reg, uint8_t *value) {
  int ret = sccb_write_bytes(&reg, 1);
  if (ret) return ret;
  return sccb_read_bytes(value, 1);
}

// Idempotent bus setup. Must run BEFORE any transaction: ov7670_detect()
// is called before ov7670_init() and both need the I2C peripheral
// configured or reads float and ACK is never seen.
static void sccb_pins_init(void) {
  gpio_set_function(SCCB_SCL, GPIO_FUNC_I2C);
  gpio_set_function(SCCB_SDA, GPIO_FUNC_I2C);
  gpio_pull_up(SCCB_SCL);
  gpio_pull_up(SCCB_SDA);
  // 10 kHz, not 100 kHz: the only pull-ups are RP2040's internal ~50k ones.
  // RC rise time (50k x ~150pF wire) ~7.5us vs 5us half-period at 100 kHz
  // would never reach a valid high. 10 kHz matches the working reference.
  i2c_init(SCCB_I2C, 10 * 1000);
}

bool ov7670_detect(void) {
  uint8_t pid = 0;
  sccb_pins_init();
  if (ov7670_read_reg(OV7670_REG_PID, &pid) != 0) {
    return false;
  }
  return pid == 0x76;
}

bool ov7670_i2c_scan(void) {
  uint8_t probe = 0;
  for (uint8_t addr = 0x01; addr < 0x7F; addr++) {
    if (addr == OV7670_I2C_ADDR) {
      continue;
    }
    if (i2c_write_blocking(SCCB_I2C, addr, &probe, 1, false) == 1) {
      return true;
    }
  }
  return false;
}

// Bus idle-level probe: bit1 = SCL level, bit0 = SDA level (1 = high).
// Temporarily switches SCCB pins to SIO inputs with pull-ups so a dead-bus
// diagnosis can distinguish a healthy idle bus (0x03) from a shorted or
// clamped one (0x00) when no address ACKs.
uint8_t ov7670_bus_levels(void) {
  gpio_set_function(SCCB_SCL, GPIO_FUNC_SIO);
  gpio_set_function(SCCB_SDA, GPIO_FUNC_SIO);
  gpio_set_dir(SCCB_SCL, GPIO_IN);
  gpio_set_dir(SCCB_SDA, GPIO_IN);
  gpio_pull_up(SCCB_SCL);
  gpio_pull_up(SCCB_SDA);
  busy_wait_us(200);
  uint8_t lvl = (gpio_get(SCCB_SCL) ? 0x02u : 0u) | (gpio_get(SCCB_SDA) ? 0x01u : 0u);
  gpio_set_function(SCCB_SCL, GPIO_FUNC_I2C);
  gpio_set_function(SCCB_SDA, GPIO_FUNC_I2C);
  return lvl;
}

// ---------------------------------------------------------------------------
// Init sequence
// ---------------------------------------------------------------------------

// {reg, value}, 0xFF-terminated. Source: mxyxbb/ov7670_init.h (CSDN, tuned
// for 24 MHz XCLK). The table is already QVGA-configured (COM7=0x14,
// COM15=0xD0, QVGA window/scaling); only the clock rows that conflict with
// the 8 MHz PIO XCLK build (CLKRC, DBLV) are applied *after* this table.
static const uint8_t OV7670_regs[][2] = {
    // Frame rate / clock
    {0x11, 0x80}, // CLKRC (overridden later for 8 MHz XCLK)
    {0x6b, 0x0a}, // DBLV PLL (overridden later)
    {0x2a, 0x00},
    {0x2b, 0x00},
    {0x92, 0x00},
    {0x93, 0x00},
    {0x3b, 0x0a},

    // Output format
    {0x12, 0x14}, // QVGA(320x240) RGB
    {0x40, 0xd0}, // COM15: RGB565, full [00]-[FF] range
    {0x8c, 0x00},

    // Special effects: normal
    {0x3a, 0x00},
    {0x67, 0x80},
    {0x68, 0x80},

    // Mirror/VFlip (adjusted later: 0x07 = no flip)
    {0x1e, 0x37},

    // Banding filter (24 MHz XCLK values; overridden later for 8 MHz)
    {0x13, 0xe7},
    {0x9d, 0x98}, // 50Hz
    {0x9e, 0x7f}, // 60Hz
    {0xa5, 0x02},
    {0xab, 0x03},
    {0x3b, 0x02},

    // Simple White Balance
    {0x13, 0xe7},
    {0x6f, 0x9f},

    // AWBC
    {0x43, 0x14},
    {0x44, 0xf0},
    {0x45, 0x34},
    {0x46, 0x58},
    {0x47, 0x28},
    {0x48, 0x3a},

    // AWB Control
    {0x59, 0x88},
    {0x5a, 0x88},
    {0x5b, 0x44},
    {0x5c, 0x67},
    {0x5d, 0x49},
    {0x5e, 0x0e},

    // AWB Control
    {0x6c, 0x0a},
    {0x6d, 0x55},
    {0x6e, 0x11},
    {0x6f, 0x9f},

    // AEC algorithm selection (average-based)
    {0xaa, 0x94},

    // Histogram-based AGC/AEC
    {0x9f, 0x78},
    {0xa0, 0x68},
    {0xa6, 0xdf},
    {0xa7, 0xdf},
    {0xa8, 0xf0},
    {0xa9, 0x90},

    // Fix Gain
    {0x69, 0x5d},

    // Color saturation
    {0x4f, 0x80},
    {0x50, 0x80},
    {0x51, 0x00},
    {0x52, 0x22},
    {0x53, 0x5e},
    {0x54, 0x80},
    {0x58, 0x9e},

    // Contrast
    {0x56, 0x40},

    // Gamma curve
    {0x7a, 0x20},
    {0x7b, 0x1c},
    {0x7c, 0x28},
    {0x7d, 0x3c},
    {0x7e, 0x55},
    {0x7f, 0x68},
    {0x80, 0x76},
    {0x81, 0x80},
    {0x82, 0x88},
    {0x83, 0x8f},
    {0x84, 0x96},
    {0x85, 0xa3},
    {0x86, 0xaf},
    {0x87, 0xc4},
    {0x88, 0xd7},
    {0x89, 0xe8},

    // Matrix coefficients
    {0x4f, 0x80},
    {0x50, 0x80},
    {0x51, 0x00},
    {0x52, 0x22},
    {0x53, 0x5e},
    {0x54, 0x80},

    // Lens correction
    {0x62, 0x00},
    {0x63, 0x00},
    {0x64, 0x04},
    {0x65, 0x20},
    {0x66, 0x05},
    {0x94, 0x04},
    {0x95, 0x08},

    // Window (QVGA values)
    {0x17, 0x16},
    {0x18, 0x04},
    {0x19, 0x02},
    {0x1a, 0x7b},
    {0x32, 0x80},
    {0x03, 0x06},

    // PCLK/HREF/VSYNC config
    {0x15, 0x02},

    // Automatic black level compensation
    {0xb0, 0x84},

    // Scaling (QVGA values)
    {0x70, 0x00},
    {0x71, 0x00},
    {0x72, 0x11},
    {0x73, 0x08},
    {0x3e, 0x00},

    // ADC
    {0x37, 0x1d},
    {0x38, 0x71},
    {0x39, 0x2a},

    // Misc
    {0x92, 0x00},
    {0xa2, 0x02},
    {0x0c, 0x0c},
    {0x10, 0x00},
    {0x0d, 0x01},
    {0x0f, 0x4b},
    {0x3c, 0x78},
    {0x74, 0x19},

    // Reserved
    {0x0e, 0x61},
    {0x16, 0x02},
    {0x21, 0x02},
    {0x22, 0x91},
    {0x29, 0x07},
    {0x33, 0x0b},
    {0x35, 0x0b},
    {0x4d, 0x40},
    {0x4e, 0x20},
    {0x8d, 0x4f},
    {0x8e, 0x00},
    {0x8f, 0x00},
    {0x90, 0x00},
    {0x91, 0x00},
    {0x96, 0x00},
    {0x9a, 0x80},

    {0xff, 0xff}, // terminator
};

static void ov7670_write_list(const uint8_t regs[][2]) {
  for (int i = 0; regs[i][0] != 0xff; i++) {
    ov7670_write_reg(regs[i][0], regs[i][1]);
    sleep_us(200); // 1ms-ish per write; SCCB needs settle time
  }
}

int ov7670_init(void) {
  sccb_pins_init();

  // Reset
  ov7670_write_reg(OV7670_REG_COM7, OV7670_COM7_RESET);
  sleep_ms(10);
  ov7670_write_list(OV7670_regs);

  // ---- Overrides for QVGA RGB565 (always win) ----
  // Clock: only the 8 MHz PIO XCLK build overrides CLKRC/DBLV. The PWM build
  // (20.8 MHz) keeps the init-table values (0x80/0x0a) verified at 24 MHz;
  // applying the 8 MHz values there pushes the internal clock past spec.
#ifndef XCLK_PWM
  ov7670_write_reg(OV7670_REG_CLKRC, 1);
  ov7670_write_reg(OV7670_REG_DBLV, 1 << 6);
#endif

  // Output format: QVGA+RGB (base), RGB565 full range
  ov7670_write_reg(OV7670_REG_COM7, OV7670_COM7_QVGA | OV7670_COM7_RGB);
  ov7670_write_reg(OV7670_REG_COM15, OV7670_COM15_R00FF | OV7670_COM15_RGB565);
  ov7670_write_reg(OV7670_REG_MVFP, 0x07); // no mirror/vflip

  // Banding filter: 8.3 MHz XCLK @ 60 Hz light uses 52/63; the -DXCLK_PWM
  // 20.8 MHz build uses the values verified by mxyxbb (0x98/0x7f).
#ifdef XCLK_PWM
  ov7670_write_reg(OV7670_REG_BD50MAX, 0x98);
  ov7670_write_reg(OV7670_REG_BD60MAX, 0x7f);
#else
  ov7670_write_reg(OV7670_REG_BD50MAX, 52);
  ov7670_write_reg(OV7670_REG_BD60MAX, 63);
#endif
  ov7670_write_reg(OV7670_REG_COM11, 1 << 3); // 50Hz
  // (banding step regs 0xA5/0xAB kept from table)

  sleep_ms(300); // tS:REG settling (~10 frames)

  return 0;
}

// ---------------------------------------------------------------------------
// Raw Bayer (640x480 8-bit) + VGA half-window switching
// ---------------------------------------------------------------------------

#ifdef RAW_BAYER_OFFICIAL_REGS
// OFFICIAL datasheet Table 2-2 Sheet 3 register set (8th group B of the A/B
// test): the exact "30 fps VGA Raw Bayer RGB mode" values from the datasheet
// (24 MHz input clock), kept verbatim. Everything NOT listed by Table 2-2
// (DBLV/COM15/MVFP/TSLB/window regs/REG74) is carried over from the shipped
// table so the A/B isolates the Table 2-2 scaling-path values only:
//   CLKRC 0x01 (vs shipped 0x80), COM14 0x00 (vs 0x18),
//   XSC 0x3A (vs 0x00), YSC 0x35 (vs 0x00),
//   DCWCTR 0x11 (vs 0x00), PCLK_DIV 0xF0 (vs 0x08 bypass).
// A/B question (T7): with per-PCLK sampling (RAW_BAYER_PER_PCLK), does the
// official register set make the sensor emit ONE distinct byte per PCLK
// (640 PCLK/line, dup_even < 1) instead of the measured 2-PCLK hold?
// Caveat: Table 2-2 targets a 24 MHz input clock; on the 20.8 MHz XCLK_PWM
// build CLKRC=0x01 halves fINT relative to 0x80 (the 'C' PCLK counter and
// dup_even are clock-rate independent, so the measurement stays decisive).
// Same soft-reset/settle handling as the shipped table below.
static const uint8_t OV7670_raw_bayer_regs[][2] = {
    {0x11, 0x01}, // CLKRC: OFFICIAL 0x01 (Table 2-2; 24 MHz input reference)
    {0x6b, 0x0a}, // DBLV: PLL bypass (kept from shipped table)
    {0x12, 0x01}, // COM7: sensor raw 8-bit Bayer out
    {0x40, 0xc0}, // COM15: full 0-255 range (no RGB565 bit — fixes D7=D0 defect)
    {0x1e, 0x07}, // MVFP: no mirror/vflip (matches shipped)
    {0x0c, 0x00}, // COM3: OFFICIAL 0x00 (= shipped)
#ifdef RAW_BAYER_COM14_V2
    {0x3e, 0x18}, // COM14: open PCLK_DIV gate (bit4+bit3=1). Without this,
                   // PCLK_DIV=0xF0 is dead and DCWCTR HDS×2 has no effect.
                   // Bit4 is the active ingredient (bit3 alone is a no-op).
#else
    {0x3e, 0x00}, // COM14: OFFICIAL 0x00 (shipped: 0x18)
#endif
    {0x3a, 0x00}, // TSLB: kept (window math requirement, see below)
    {0x17, 0x11}, // HSTART (window, kept from shipped)
    {0x18, 0x61}, // HSTOP
    {0x32, 0x80}, // HREF
    {0x19, 0x03}, // VSTART
    {0x1a, 0x7b}, // VSTOP
    {0x03, 0x03}, // VREF
    {0x70, 0x3a}, // SCALING_XSC: OFFICIAL 0x3A (shipped: 0x00 scaler bypass)
    {0x71, 0x35}, // SCALING_YSC: OFFICIAL 0x35
    {0x72, 0x11}, // SCALING_DCWCTR: OFFICIAL 0x11 (shipped: 0x00 no downsampling)
    {0x73, 0xf0}, // SCALING_PCLK_DIV: OFFICIAL 0xF0 (shipped: 0x08 bypass)
    {0x74, 0x20}, // REG74: 1x horizontal ratio (kept; not in Table 2-2)
    {0xa2, 0x02}, // SCALING_PCLK_DELAY: OFFICIAL 0x02 (= shipped)
    {0xff, 0xff},
};
#else
// Pinned raw-Bayer register set (datasheet Table 2-2 + COM7=0x01), 0xFF-term.
// Written after soft reset; 200us settle per write like OV7670_regs.
// T7 root-cause verdict (measured 2026-08-14): the 100% horizontal 2x byte
// duplication is NOT register-fixable — SEVEN live-tested configs all kept
// dup_even=1.000: XSC/YSC 0x00, PCLK_DIV 0xF0, DCWCTR 0x00, COM14 0x18,
// PCLK_DIV 0x08 (bit3=1 bypass), REG74 0x20 (1x), and exp7 minimal table
// (CLKRC 0x01 + COM14 0x08 + DCWCTR 0x11 + 0x73 0x00). In raw mode
// (COM7=0x01) per-PCLK sampling duplicates every byte; sampling every 2nd
// PCLK recovers 640 distinct bytes/line — interpreted as each byte held for
// 2 PCLKs over a 1280-PCLK line. The RGB565 path (COM7=0x14) with identical
// scaling regs does NOT dup -> sensor-side raw property. NOTE: this 2-PCLK
// hold is a MEASURED phenomenon with NO datasheet explanation — Table 6-3's
// "2 PCLK/byte" applies only to the 1/2x..1/4x horizontal-scaling band
// (REG74=0x20 => 1x => 1 PCLK/byte), and Table 3-3's official raw timing is
// 640 PCLK/line (raw PCLK = fINT/2). 1280 PCLK/line is inferred (dup_even +
// 640 distinct bytes), not directly counted; a PIO PCLK-edge counter per
// HREF line is the pending decisive measurement. The fix lives in the PIO
// program: sample every 2ND PCLK rising edge (camera.pio 6-instr loop) ->
// recovers all 640 distinct bytes/line.
// Verified live: dup_even 1.000->0.385; clean Bayer same-plane signature at
// 640 (colLag2=0.426 >> colLag1=0.146, rowLag2=0.442), absent at 320 reshape
// -> FRAME_W=640 kept, no geometry change. Cross-frame corr: NEW upper ~0.66 /
// lower ~0.87 vs OLD 0.29/0.44 (more frame-stable). An earlier byte-level
// audit (2026-08-14) inferred "320 distinct px/line" from the every-PCLK
// capture — that inference was WRONG (duplicated bytes made a 640-sample row
// look like ~320 distinct values); the every-2nd-PCLK experiment supersedes it.
// Registers below are the best-known config (test_01 readback asserts them);
// CLKRC 0x80 is build-correct (fINT = XCLK/2 on the 20.8 MHz XCLK_PWM build;
// 0x01 halves fINT). SCALING_XSC/YSC = 0x00 (scaler bypass; COM3[3]=0
// digital-zoom bypass, so XSC/YSC are don't-care).
// MVFP written explicitly: sensor power-on default 0x01 differs from the 0x07
// the readback test asserts.
static const uint8_t OV7670_raw_bayer_regs[][2] = {
    {0x11, 0x80}, // CLKRC: fINT = XCLK/2 (verified 20.8 MHz PWM build)
    {0x6b, 0x0a}, // DBLV: PLL bypass
    {0x12, 0x01}, // COM7: sensor raw 8-bit Bayer out
    {0x40, 0xc0}, // COM15: full 0-255 range (no RGB565 bit — fixes D7=D0 defect)
    {0x1e, 0x07}, // MVFP: no mirror/vflip (matches RGB565 path)
    {0x0c, 0x00}, // COM3
    {0x3e, 0x18}, // COM14: bit4+bit3 open the 0x73 gate; bits[2:0]=000 PCLK /1
    {0x3a, 0x00}, // TSLB
    {0x17, 0x11}, // HSTART
    {0x18, 0x61}, // HSTOP
    {0x32, 0x80}, // HREF
    {0x19, 0x03}, // VSTART
    {0x1a, 0x7b}, // VSTOP
    {0x03, 0x03}, // VREF
    {0x70, 0x00}, // SCALING_XSC: scaler bypass (matches RGB565 path)
    {0x71, 0x00}, // SCALING_YSC
    {0x72, 0x00}, // SCALING_DCWCTR: NO down sampling (HDS=00, VDS=00);
                  // default 0x11 = HDS by 2 (Table 6-2 line 1986), raw VGA
                  // needs the full row. NOTE (T7 verdict): dup fix is the PIO
                  // every-2nd-PCLK sampling, NOT these registers.
    {0x73, 0x08}, // SCALING_PCLK_DIV: bit[3]=1 bypass divider (matches RGB565)
    {0x74, 0x20}, // REG74: Horizontal Scaling Ratio 0x20/REG74[6:0]; 0x20=1x (Table 6-1)
    {0xa2, 0x02}, // SCALING_PCLK_DELAY
    {0xff, 0xff},
};
#endif // RAW_BAYER_OFFICIAL_REGS

int ov7670_init_raw_bayer(void) {
  sccb_pins_init();

  // Reset
  ov7670_write_reg(OV7670_REG_COM7, OV7670_COM7_RESET);
  sleep_ms(10);
  ov7670_write_list(OV7670_raw_bayer_regs);

  // tS:REG settling (~10 frames): let AGC/AEC converge on the scene first,
  // THEN freeze them. The upper/lower half-frames are captured seconds apart
  // as two independent acquisitions; with auto-exposure still enabled (COM8
  // reset default 0xE7 = FASTAEC|AECSTEP|BANDING|AGC|AEC|AWB) the AGC engine
  // keeps re-converging between the two, producing a brightness jump at the
  // stitch seam (measured seam row diff up to 128/255 on live data). Clearing
  // COM8 bits 2/1 (AGC+AEC) leaves 0xE1 (FASTAEC|AECSTEP|BANDING|AWB): the
  // GAIN/AECH registers are driven by the auto-exposure engine and are
  // read-only while it runs, so disabling AGC+AEC freezes them at the values
  // converged above — both halves then share one locked exposure. AWB is kept
  // on (raw Bayer output already bypasses most color processing; AWB only
  // nudges R/B gains and does not cause the seam brightness jump).
  ov7670_write_reg(OV7670_REG_COM8, 0xE1);

  sleep_ms(100); // register settle after the lock write
  return 0;
}

// Window math (datasheet): VSTRT=(VSTART<<2)|VREF[1:0]; VSTOP=(VSTOP<<2)|VREF[3:2].
// MEASURED (2026-08-14): VSTOP is EXCLUSIVE — a window (VSTRT,VSTOP) delivers
// rows VSTRT..VSTOP-1. With the old upper VREF[3:0]=0x3 (VSTOP_eff=252) the
// upper frame delivered only 237 valid rows (15..251); capture rows 237..239
// were the NEXT frame's top rows (wrap-around, corr 0.76-0.85 to upper[0..2]),
// NOT the assumed "overlap 252..254" — that is the root cause of the bright
// stitch seam (blending bright wrap rows into the dark lower window).
// Pinned values (corrected):
//   upper: VSTART=0x03,VSTOP=0x3F,VREF[3:0]=0xF -> rows 15..254 (240 rows,
//          REAL 3-row overlap 252..254 with lower; VSTOP_eff=252|3=255)
//   lower: VSTART=0x3F,VSTOP=0x7B,VREF[3:0]=0x0 -> rows 252..491 (240 rows,
//          exact fit, no wrap)
// TSLB[0]=0 MUST be written before any window-register write.
int ov7670_set_bayer_window(uint8_t half) {
  uint8_t vstart, vstop, vref_lo;
  if (half == OV7670_BAYER_WINDOW_UPPER) {
    vstart = 0x03;
    vstop = 0x3f;
    vref_lo = 0x0f;  // VSTOP_eff=255 -> rows 15..254 (240 rows, real 252..254 overlap)
  } else if (half == OV7670_BAYER_WINDOW_LOWER) {
    vstart = 0x3f;
    vstop = 0x7b;
    vref_lo = 0x00;
  } else {
    return -1;
  }

  if (ov7670_write_reg(OV7670_REG_TSLB, 0x00) != 0) return -1;

  // VREF read-modify-write: preserve bits[7:4] (AGC high bits)
  uint8_t vref = 0;
  if (ov7670_read_reg(OV7670_REG_VREF, &vref) != 0) return -1;
  vref = (uint8_t)((vref & 0xF0) | vref_lo);

  if (ov7670_write_reg(OV7670_REG_VSTART, vstart) != 0) return -1;
  if (ov7670_write_reg(OV7670_REG_VSTOP, vstop) != 0) return -1;
  if (ov7670_write_reg(OV7670_REG_VREF, vref) != 0) return -1;
  sleep_us(200);

  return 0;
}

// ---------------------------------------------------------------------------
// 320x240 centered window (single-window, no 'T' switching)
// ---------------------------------------------------------------------------

// Set a 320x240 window centered in the 640x480 sensor.
// Horizontal: 320 pixels centered in 640 = offset 160 from left
//   HSTART=0x25 (160/4=40), HSTOP=0x4D (480/4=120), HREF[2:0]=0, HREF[5:3]=0
// Vertical: 240 pixels centered in 480 = offset 120 from top
//   VSTART=0x1E (120/4=30), VSTOP=0x5A (360/4=90), VREF_lo=0x00
int ov7670_set_bayer_window_320x240(void) {
  if (ov7670_write_reg(OV7670_REG_TSLB, 0x00) != 0) return -1;

  // HSTRT=(0x11<<2)|0=68, HSTOP=(0x61<<2)|0=388, width=320
  if (ov7670_write_reg(0x17, 0x11) != 0) return -1;
  if (ov7670_write_reg(0x18, 0x61) != 0) return -1;
  if (ov7670_write_reg(0x32, 0x80) != 0) return -1;

  // Vertical: center 240px in 480px sensor
  //   VSTRT=(0x1E<<2)|0=120, VSTOP=(0x5A<<2)|0=360, rows 120..359
  uint8_t vref = 0;
  if (ov7670_read_reg(OV7670_REG_VREF, &vref) != 0) return -1;
  vref = (uint8_t)((vref & 0xF0) | 0x00);

  if (ov7670_write_reg(OV7670_REG_VSTART, 0x1E) != 0) return -1;
  if (ov7670_write_reg(OV7670_REG_VSTOP, 0x5A) != 0) return -1;
  if (ov7670_write_reg(OV7670_REG_VREF, vref) != 0) return -1;

  sleep_us(200);
  return 0;
}
