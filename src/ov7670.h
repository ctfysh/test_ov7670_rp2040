#ifndef OV7670_H
#define OV7670_H

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// ---- Wiring (user-confirmed) ----
#ifdef SCCB_SWAP
#define OV7670_PIN_SIOD 21 // SDA swapped for reversed-wiring test
#define OV7670_PIN_SIOC 20 // SCL
#else
#define OV7670_PIN_SIOD 20 // SDA, 4.7k pull-up
#define OV7670_PIN_SIOC 21 // SCL, 4.7k pull-up
#endif
#define OV7670_PIN_RESET -1 // RESET -> 3V3 (or GP26)
#define OV7670_PIN_PWDN -1 // PWDN  -> GND
#define OV7670_PIN_XCLK 22 // 22 MHz square wave from PWM

#define OV7670_I2C_ADDR 0x21 // 0x42 >> 1

// ---- Register map ----
#define OV7670_REG_GAIN 0x00
#define OV7670_REG_BLUE 0x01
#define OV7670_REG_RED 0x02
#define OV7670_REG_VREF 0x03
#define OV7670_REG_COM1 0x04
#define OV7670_REG_PID 0x0A
#define OV7670_REG_VER 0x0B
#define OV7670_REG_COM3 0x0C
#define OV7670_REG_COM4 0x0D
#define OV7670_REG_COM5 0x0E
#define OV7670_REG_COM6 0x0F
#define OV7670_REG_AECH 0x10
#define OV7670_REG_CLKRC 0x11
#define OV7670_REG_COM7 0x12
#define OV7670_REG_COM8 0x13
#define OV7670_REG_COM9 0x14
#define OV7670_REG_COM10 0x15
#define OV7670_REG_HSTART 0x17
#define OV7670_REG_HSTOP 0x18
#define OV7670_REG_VSTART 0x19
#define OV7670_REG_VSTOP 0x1A
#define OV7670_REG_PSHFT 0x1B
#define OV7670_REG_MVFP 0x1E
#define OV7670_REG_AEW 0x24
#define OV7670_REG_AEB 0x25
#define OV7670_REG_VPT 0x26
#define OV7670_REG_HREF 0x32
#define OV7670_REG_TSLB 0x3A
#define OV7670_REG_COM11 0x3B
#define OV7670_REG_COM12 0x3C
#define OV7670_REG_COM13 0x3D
#define OV7670_REG_COM14 0x3E
#define OV7670_REG_COM15 0x40
#define OV7670_REG_COM16 0x41
#define OV7670_REG_MTX1 0x4F
#define OV7670_REG_MTX2 0x50
#define OV7670_REG_MTX3 0x51
#define OV7670_REG_MTX4 0x52
#define OV7670_REG_MTX5 0x53
#define OV7670_REG_MTX6 0x54
#define OV7670_REG_BRIGHT 0x55
#define OV7670_REG_CONTRAS 0x56
#define OV7670_REG_CONTRAS_CENTER 0x57
#define OV7670_REG_GFIX 0x69
#define OV7670_REG_DBLV 0x6B
#define OV7670_REG_AWBCTR0 0x6F
#define OV7670_REG_SCALING_XSC 0x70
#define OV7670_REG_SCALING_YSC 0x71
#define OV7670_REG_SCALING_DCWCTR 0x72
#define OV7670_REG_SCALING_PCLK_DIV 0x73
#define OV7670_REG_REG74 0x74
#define OV7670_REG_GAM_BASE 0x7A // GAM1..GAM15 = 0x7A..0x88
#define OV7670_REG_BD50MAX 0x9D
#define OV7670_REG_BD60MAX 0x9E
#define OV7670_REG_HAECC1 0x9F
#define OV7670_REG_HAECC2 0xA0
#define OV7670_REG_SCALING_PCLK_DELAY 0xA2
#define OV7670_REG_HAECC3 0xA5
#define OV7670_REG_HAECC4 0xA6
#define OV7670_REG_HAECC5 0xA7
#define OV7670_REG_HAECC6 0xA8
#define OV7670_REG_HAECC7 0xA9
#define OV7670_REG_HAECC8 0xAA
#define OV7670_REG_ABLC1 0xB0
#define OV7670_REG_THL_ST 0xB3

// COM7 bits
#define OV7670_COM7_RESET 0x80
#define OV7670_COM7_FMT_MASK 0x0C
#define OV7670_COM7_RGB 0x04
#define OV7670_COM7_YUV 0x00
#define OV7670_COM7_SIZE_MASK 0x38
#define OV7670_COM7_VGA 0x00
#define OV7670_COM7_QVGA 0x10
#define OV7670_COM7_SENSOR_RAW 0x01 // sensor raw 8-bit Bayer data out

// COM15 bits
#define OV7670_COM15_R00FF 0xC0 // full 0-255 output range
#define OV7670_COM15_RGB565 0x10

// COM3 bits
#define OV7670_COM3_DCWEN 0x04 // downsample enable
#define OV7670_COM3_SCALEEN 0x08 // zoom enable

// COM8 bits
#define OV7670_COM8_FASTAEC 0x80
#define OV7670_COM8_AECSTEP 0x40
#define OV7670_COM8_BANDING 0x20
#define OV7670_COM8_AGC 0x04
#define OV7670_COM8_AEC 0x02
#define OV7670_COM8_AWB 0x01

// Output sizes (index into window table)
#define OV7670_SIZE_DIV1 0 // 640x480 VGA
#define OV7670_SIZE_DIV2 1 // 320x240 QVGA
#define OV7670_SIZE_DIV4 2 // 160x120 QQVGA
#define OV7670_SIZE_DIV8 3 // 80x60
#define OV7670_SIZE_DIV16 4 // 40x30

// ---- API ----
// Low-level SCCB register access
int ov7670_write_reg(uint8_t reg, uint8_t value);
int ov7670_read_reg(uint8_t reg, uint8_t *value);

// Returns true if PID register reads 0x76
bool ov7670_detect(void);

// Returns true if ANY 7-bit address other than OV7670_I2C_ADDR ACKs.
// Used to distinguish a dead bus from a camera at a different address.
bool ov7670_i2c_scan(void);

// Idle bus-level probe, bit1=SCL, bit0=SDA (1=high). 0x03 = healthy idle
// bus, 0x00 = shorted/clamped. Call only after a failed detect.
uint8_t ov7670_bus_levels(void);

// Full bring-up: reset, RGB565 format, QVGA 320x240.
// Returns 0 on success.
int ov7670_init(void);

// Bayer half-window selectors (ov7670_set_bayer_window).
// VSTOP is exclusive (measured): upper window VSTOP_eff=255 -> rows 15..254,
// lower rows 252..491 -> real 3-row overlap (252..254) between the halves.
#define OV7670_BAYER_WINDOW_UPPER 0 // rows 15..254
#define OV7670_BAYER_WINDOW_LOWER 1 // rows 252..491

// Full bring-up: reset, raw Bayer VGA 640x480 8-bit (Table 2-2 + COM7=0x01).
// Returns 0 on success.
int ov7670_init_raw_bayer(void);

// Switch vertical capture window: upper (rows 15..254) or lower (252..491).
// Returns 0 on success, -1 on invalid half or SCCB failure.
int ov7670_set_bayer_window(uint8_t half);

// Set a 320x240 window centered in the 640x480 sensor (single-window mode).
// Returns 0 on success.
int ov7670_set_bayer_window_320x240(void);

#ifdef __cplusplus
}
#endif

#endif // OV7670_H
