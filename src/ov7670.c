// OV7670 no-FIFO driver for YD-RP2040
// - SCCB via hardware I2C0 on SIOC=GP21 / SIOD=GP20, 4.7k pull-ups
// - QVGA 320x240 YUV422 YUYV output (UVC YUY2 byte order)
// - XCLK = 20.8 MHz hardware PWM (add -DXCLK_PWM) or 8 MHz PIO (default)
//
// Register/format strategy:
//   1. Soft reset (COM7 = 0x80)
//   2. CLKRC/DBLV for 8 MHz XCLK (PIO build only; PWM build keeps table)
//   3. Known-good full init table (CSDN, tuned for 24 MHz XCLK, QVGA config)
//   4. Override format to YUV422 YUYV + QVGA (window/scaling regs already QVGA
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
    {0x12, 0x10}, // QVGA(320x240) YUV
    {0x40, 0xc0}, // COM15: full [00]-[FF] range (no RGB565)
    {0x3d, 0x80}, // COM13: YUYV output order (UVC YUY2)
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

  // ---- Overrides for QVGA YUV422 YUYV (always win) ----
  // Clock: only the 8 MHz PIO XCLK build overrides CLKRC/DBLV. The PWM build
  // (20.8 MHz) keeps the init-table values (0x80/0x0a) verified at 24 MHz;
  // applying the 8 MHz values there pushes the internal clock past spec.
#ifndef XCLK_PWM
  ov7670_write_reg(OV7670_REG_CLKRC, 1);
  ov7670_write_reg(OV7670_REG_DBLV, 1 << 6);
#endif

  // Output format: QVGA+YUV (base), full range, YUYV order
  ov7670_write_reg(OV7670_REG_COM7, OV7670_COM7_QVGA | OV7670_COM7_YUV);
  ov7670_write_reg(OV7670_REG_COM15, OV7670_COM15_R00FF);
  ov7670_write_reg(OV7670_REG_COM13, OV7670_COM13_YUYV);
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
