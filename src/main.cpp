// OV7670 no-FIFO camera -> USB CDC video stream on YD-RP2040
//
// Flow:
//   XCLK on GP22: hardware PWM 20.8 MHz (add -DXCLK_PWM to build_flags; this
//                 build is the one shipped here) OR PIO0 SM0 8.3 MHz default
//   PIO0 SM1: capture 8-bit pixel bytes on GP8..GP15 (HREF=GP17, PCLK=GP18)
//   VSYNC (GP16) rising edge IRQ starts a DMA transfer of one full frame
//   DMA single-buffer FRAME_BYTES; on completion the frame is flagged
//   loop() streams the completed frame over USB CDC (Serial)
//
// Frame size: QVGA 320x240 RGB565 = 153600 bytes (FRAME_W/FRAME_H in
// platformio.ini). RAW_BAYER mode: 320x240 raw Bayer 8-bit (1 byte/px,
// 76800 bytes), "CAM2" magic. Default build uses official Table 2-2
// registers + per-PCLK sampling (DCWCTR=0x11 HDS×2 → 320 distinct
// bytes/line matches FRAME_W=320). Legacy shipped build (every-2nd-PCLK)
// outputs 640 distinct bytes/line at 320 FRAME_W — half dropped.
// Host protocol (matches capture.py):
//   "CAM1" (4 B) + W (u16 BE) + H (u16 BE) + FRAME_BYTES raw RGB565.
//   "CAM2" (4 B) + W (u16 BE) + H (u16 BE) + FRAME_BYTES raw Bayer 8-bit.
//   capture.py/bayer_capture.py sync by scanning for the magic, so every
//   diagnostic packet below uses a distinct "DBG1" magic that the host passes
//   through.

#include <Arduino.h>

#include "camera.pio.h"
#include "ov7670.h"

#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/pwm.h"
#include "pico/bootrom.h"
#include "pico/stdlib.h"

// ---- Pin / config (USB-safe; PIO IN_BASE must be a multiple of 8) ----
#define PIN_D0 8  // D0..D7 = GP8..GP15 (in_base=8, avoids USB GP0/GP1)
#define PIN_VSYNC 16
#define PIN_HREF 17
#define PIN_PCLK 18
#define PIN_XCLK 22

#define CAP_PIO pio0
#define SM_XCLK 0
#define SM_CAPTURE 1
#define SM_COUNT 2 // diagnostic PCLK counter (pclk_count program, 'C' cmd)

// Frame bytes: 1 byte/px in RAW_BAYER (320x240 = 76800), 2 bytes/px RGB565.
#ifdef RAW_BAYER
#define FRAME_BYTES (FRAME_W * FRAME_H)     // 320*240*1 = 76800
#else
#define FRAME_BYTES (FRAME_W * FRAME_H * 2) // 320*240*2 = 153600
#endif

// ---- Frame protocol over USB (CAM1: magic + W + H + raw RGB565) ----
// Pixels are BIG-ENDIAN per pair (OV7670 emits the high byte first; the PIO
// preserves byte order into the DMA buffer). Host tools must parse
// (b[i] << 8) | b[i+1], NOT the little-endian (b[i] | b[i+1] << 8) which
// swaps R and B and scrambles G.
#define FRAME_MAGIC0 'C'
#define FRAME_MAGIC1 'A'
#define FRAME_MAGIC2 'M'
#ifdef RAW_BAYER
#define FRAME_MAGIC3 '2' // CAM2: raw Bayer 8-bit
#else
#define FRAME_MAGIC3 '1' // CAM1: RGB565
#endif
// Diagnostic packets use a different magic so capture.py's "CAM1" sync
// loop passes them through without ever matching.
#define DBG_MAGIC0 'D'
#define DBG_MAGIC1 'B'
#define DBG_MAGIC2 'G'
#define DBG_MAGIC3 '1'

// ---- DMA single buffer ----
// 2 x FRAME_BYTES (307200 B) would exceed the RP2040's 264 KB SRAM, so the
// frame is captured into one buffer and streamed out. The buffer is only
// written by DMA between frame_ready=false and the next VSYNC, so the host
// never reads a buffer that is being overwritten.
static uint8_t frames[FRAME_BYTES];
static volatile int dma_chan = -1; // DMA channel for capture
static volatile bool frame_ready = false; // frames[] holds a complete frame
static volatile bool dma_busy = false; // DMA armed, waiting for completion IRQ
static dma_channel_config capture_cfg; // config re-used to re-arm each frame

// ---- Diagnostic markers (sent as DBG1 + marker + payload) ----
// 0xFF = camera not detected on SCCB   (retried every 500ms)
// 0xFE = camera detected, init done    (sent once)
// 0xFD = no VSYNC frame for >3s        (retried every 500ms)
// 0xFD packet (21 B): DBG1 + 0xFD + 4 activity probes (VSYNC/HREF/PCLK/D0)
//   + 2 edge probes (VSYNC/HREF) + 6 register readbacks (COM7/COM15/COM10/
//   CLKRC/MVFP/DBLV). Edge probes settle "stuck high" vs "active-low pulse".
// On-chip state tail (15 B, appended since instrumenting):
//   SM PC (1) + RX FIFO level (1) + DMA remaining count (4 BE) +
//   DMA ctrl_trig full (4 BE) + dma_chan signed (1) +
//   vsync IRQ hits (2 BE) + dma IRQ hits (2 BE)
//   PC = SM instruction 0..3 (pc - cap_off); FIFO 0..8 words (8=full);
//   DMA count 153600 = never ran / never progressed, 0 = completed;
//   ctrl_trig bit0 = BUSY (EN reads back), [3:2] DATA_SIZE (0=byte),
//   bit4 INCR_READ, bit5 INCR_WRITE, [20:15] TREQ_SEL (5 = PIO0 RX1,
//   0x1F = FORCE); dma_chan 0xFF = -1 = dma_setup() never completed.
static bool cam_ready = false;
static uint32_t last_frame_ms = 0;

// ---- On-chip state counters (diagnostic) ----
// vsync_irq_count: times vsync_isr ran. 0 = VSYNC edge IRQ never fires, so
//   the DMA is never armed and the SM stalls with a full RX FIFO.
// dma_done_count:  times the DMA completion IRQ fired. >0 means the DMA did
//   run to completion at least once (then the re-arm path is the suspect).
static volatile uint32_t vsync_irq_count = 0;
static volatile uint32_t dma_done_count = 0;
static uint cap_off = 0; // capture program offset in PIO (pc - cap_off = 0..3)

// ---- VSYNC-edge ring diagnostic ('V' command) ----
// Ground truth for the intermittent rotated-frame bug (bad frames show the
// window content rotated by K rows with WRAP: fr[r] ~= ref[(r+off)%240]).
// Every VSYNC rising edge records one byte of pipeline state BEFORE any
// re-arm side effects:
//   bit7  = DMA re-armed this edge (guard && HREF-low >= VBLANK_GATE_US)
//   bit6:5 = SM RX FIFO level (0..4 words; 4 = SM stalled on push)
//   bit4:2 = capture SM PC - cap_off (0..5; 0 = waiting on HREF high)
//   bit1  = PCLK level at the edge
//   bit0  = HREF level at the edge
// The old invariant "every arming edge has HREF=0 and PC=0 => alignment
// clean" was FALSE: hblank edges (~14 us HREF-low between lines) satisfy
// both but arm the DMA mid-window, producing the rotated frames. The fix
// (vsync_isr) additionally requires HREF-low >= VBLANK_GATE_US at the edge,
// so bit7 now means the edge was inside the vblank (window not started) and
// the capture is aligned by construction.
// Reply: DBG1 + 0xF7 + idx(4 BE, total edges since boot) + VSYNC_RING_LEN *
// (state byte + ts u16 BE, ts = time_us_32() >> 6 so 64us resolution, wraps
// 4.19 s; the host uses ts spacing to verify frame<->edge mapping).
// 1024 entries = 27.2 s at the 37.6 fps sensor rate, comfortably covering a
// 12-frame USB-limited capture (~10 s+); the old 128-entry ring wrapped out
// the early arming edges (only 2 of 12 frames mapped).
#define VSYNC_RING_LEN 1024
static volatile uint8_t vsync_ring[VSYNC_RING_LEN];
static volatile uint16_t vsync_ring_ts[VSYNC_RING_LEN];
static volatile uint32_t vsync_ring_i = 0;

// ---- VSYNC/HREF edge IRQ: frame timing + vblank-gated DMA re-arm ----
// Rotated-frame root cause (fixed): during multi-fire periods the VSYNC line
// bursts with spurious edges, all inside HREF=0 gaps. The per-line hblank
// (~14 us, measured) is also HREF=0, so an edge there passed the old guard
// (!frame_ready && !dma_busy), armed the DMA mid-window, and the capture SM
// (line gate `wait 1 gpio 17`, a LEVEL wait) resumed at the next line ->
// the frame comes out rotated by K rows with wrap. The true frame edge fires
// 700-800 us into the vblank (measured via 'S'), so gating arming on
// HREF-low >= VBLANK_GATE_US rejects every hblank edge (~14 us) and every
// line-active edge (HREF=1) while always passing the real frame edge. Any
// arming inside vblank is safe: the window's first line has not started.
#define VBLANK_GATE_US 512
static volatile uint32_t last_href_fall_us = 0; // ts of last HREF falling edge

static void vsync_isr(uint gpio, uint32_t events) {
  if (gpio == PIN_HREF && (events & GPIO_IRQ_EDGE_FALL)) {
    last_href_fall_us = time_us_32();
    return;
  }
  if (gpio != PIN_VSYNC || !(events & GPIO_IRQ_EDGE_RISE)) return;
  vsync_irq_count++;
  int chan = dma_chan;
  // Record pipeline state at THIS edge (before any re-arm side effects).
  // bit7 = actually re-armed = guard && vblank_ok.
  bool vblank_ok =
      (uint32_t)(time_us_32() - last_href_fall_us) >= VBLANK_GATE_US;
  bool guard = !frame_ready && !dma_busy && vblank_ok;
  uint8_t pc = (uint8_t)(pio_sm_get_pc(CAP_PIO, SM_CAPTURE) - cap_off);
  uint8_t fifo = (uint8_t)pio_sm_get_rx_fifo_level(CAP_PIO, SM_CAPTURE);
  uint8_t e = (uint8_t)((guard ? 0x80 : 0x00) | ((fifo & 0x03) << 5) |
                        ((pc & 0x07) << 2) |
                        (gpio_get(PIN_PCLK) ? 0x02 : 0x00) |
                        (gpio_get(PIN_HREF) ? 0x01 : 0x00));
  uint32_t idx = vsync_ring_i++;
  vsync_ring[idx & (VSYNC_RING_LEN - 1)] = e;
  vsync_ring_ts[idx & (VSYNC_RING_LEN - 1)] =
      (uint16_t)(time_us_32() >> 6);
  if (chan < 0) return;
  // Start a new capture only if the previous DMA finished (tracked with a
  // software flag - the HW EN/BUSY readback is not a reliable "completed"
  // indicator: the config write at setup leaves EN=1 forever, which blocked
  // this guard and meant the channel was never triggered) and the previous
  // frame has already been consumed by loop().
  if (guard) {
    // Drop any residual bytes left in the RX FIFO from the previous frame so
    // the new frame starts pixel-aligned.
    pio_sm_clear_fifos(CAP_PIO, SM_CAPTURE);
    dma_busy = true;
    // Full re-configure with trigger: reloads the transfer count (consumed
    // by the previous run) and starts the channel immediately.
    dma_channel_configure(chan, &capture_cfg, frames,
                          &CAP_PIO->rxf[SM_CAPTURE], FRAME_BYTES, true);
  }
}

// ---- DMA complete: frame is ready to send ----
static void dma_isr(void) {
  if (dma_hw->intr & (1u << dma_chan)) {
    dma_hw->intr = 1u << dma_chan;
    dma_done_count++;
    dma_busy = false;
    frame_ready = true;
  }
}

static void dma_setup(void) {
  dma_chan = dma_claim_unused_channel(true);
  capture_cfg = dma_channel_get_default_config(dma_chan);
  channel_config_set_read_increment(&capture_cfg, false); // PIO RX FIFO
  channel_config_set_write_increment(&capture_cfg, true); // into buffer
  channel_config_set_dreq(&capture_cfg, pio_get_dreq(CAP_PIO, SM_CAPTURE, false));
  channel_config_set_transfer_data_size(&capture_cfg, DMA_SIZE_8);
  dma_channel_configure(dma_chan, &capture_cfg, frames,
                        &CAP_PIO->rxf[SM_CAPTURE], FRAME_BYTES, false);
  irq_set_exclusive_handler(DMA_IRQ_0, dma_isr);
  irq_set_enabled(DMA_IRQ_0, true);
  dma_channel_set_irq0_enabled(dma_chan, true);
}

static void xclk_start(void) {
#ifdef XCLK_PWM
  // XCLK via hardware PWM: 125 MHz / 6 = 20.83 MHz. Kept below the 24 MHz
  // XCLK spec: with the init table's PLL (DBLV=0x0A, x2) the internal clock
  // is 41.7 MHz < 48 MHz limit. 25 MHz (wrap=4) pushed it to 50 MHz -> the
  // sensor FSM stalled (VSYNC stuck high, no HREF rows, PCLK still toggling).
  // Same GP22 as the PIO version - wiring unchanged.
  gpio_set_function(PIN_XCLK, GPIO_FUNC_PWM);
  uint slice = pwm_gpio_to_slice_num(PIN_XCLK);
  pwm_set_wrap(slice, 5); // counter 0..5 = 6 cycles
  pwm_set_gpio_level(PIN_XCLK, 2); // ~40% duty
  pwm_set_enabled(slice, true);
#else
  // XCLK generator (8 MHz out of system clock)
  uint xclk_off = pio_add_program(CAP_PIO, &xclk_program);
  xclk_program_init(CAP_PIO, SM_XCLK, xclk_off, PIN_XCLK);
#endif
}

// Sample GP22 while XCLK runs; returns high count over 100 reads (~50 means
// the clock is toggling, 0 or 100 means no output). Used to rule out a dead
// XCLK when the camera never ACKs on SCCB.
static uint8_t xclk_activity_probe(void) {
  uint8_t hi = 0;
  for (int i = 0; i < 100; i++) {
    if (gpio_get(PIN_XCLK)) hi++;
  }
  return hi;
}

// Sample a GPIO over ~100ms (100 x 1ms); returns high count. 0 = stuck low,
// 100 = stuck high, middle values = pulsing (fraction ~ duty cycle).
static uint8_t gpio_activity_probe(uint pin) {
  uint16_t hi = 0;
  for (int i = 0; i < 100; i++) {
    if (gpio_get(pin)) hi++;
    busy_wait_us(1000);
  }
  return (uint8_t)hi;
}

// Count GPIO edges over ~100ms (10000 x ~10us samples). The 1ms level probe
// cannot tell a truly stuck-high VSYNC from normal active-low pulsing (a
// ~100us sync pulse sampled 1x/ms looks like 97-100). Edges settle it:
// 0 = dead, 2-20 = frame timing is flowing (1 pulse/frame = 2 edges).
// HREF at QVGA hits hundreds of edges per frame, so cap at 255.
static uint8_t gpio_edge_probe(uint pin) {
  uint32_t edges = 0;
  bool prev = gpio_get(pin);
  for (int i = 0; i < 10000; i++) {
    bool cur = gpio_get(pin);
    if (cur != prev) {
      edges++;
      if (edges >= 255) return 255;
      prev = cur;
    }
    busy_wait_us(10);
  }
  return (uint8_t)edges;
}

// Read a register for diagnostics; 0xFF marks a failed/faulted read so the
// host can tell a dropped write from a dead bus.
static uint8_t read_reg_checked(uint8_t reg) {
  uint8_t v = 0;
  return ov7670_read_reg(reg, &v) == 0 ? v : 0xFF;
}

// ---- Register readback (diagnostic) ----
// Host sends 'R' over USB; MCU replies with
//   DBG1 + 0xFB + count + count * (reg, val)
// Picks the registers that settle the format and AGC/AEC questions from the
// current init table. A value of 0xFF means that SCCB read failed (dropped
// init write or dead bus). GAIN (0x00) and AECH (0x10) are live auto-exposure
// state: they are read-only on the OV7670 and show whether AGC/AEC is
// pumping the image while the scene is constant.
#define REG_MARKER 0xFB

#ifdef RAW_BAYER
// 'T' handler removed - using single 320x240 window, no half-frame switching
#endif // RAW_BAYER

static void reg_readback_send(void) {
  static const uint8_t regs[] = {
#ifdef RAW_BAYER
      0x0A, // PID          expect 0x76 (OV7670)
      0x0B, // VER          expect 0x73
      0x12, // COM7         expect 0x01 (sensor raw)
      0x40, // COM15        expect 0xC0 (full range, no RGB565 — fixes D7=D0)
      0x15, // COM10        live
      0x11, // CLKRC        expect 0x80 (20.8 MHz build; fINT = XCLK/2)
      0x6B, // DBLV         expect 0x0A (PLL)
      0x1E, // MVFP         expect 0x07 (no flip)
      0x13, // COM8         expect 0xE1 (AGC+AEC locked for seam-free stitching)
      0x17, // HSTART       expect 0x11
      0x18, // HSTOP        expect 0x61
      0x19, // VSTART       expect 0x03 (full window after init)
      0x1A, // VSTOP        expect 0x7B
      0x03, // VREF         expect 0x03
      0x32, // HREF         expect 0x80
      0x70, // SCALING_XSC  expect 0x00 (scaler bypass; matches RGB565 path)
      0x71, // SCALING_YSC  expect 0x00 (scaler bypass)
      0x0C, // COM3         expect 0x00 (zoom/downsampling bypass)
      0x3E, // COM14        expect 0x18 (bit4+bit3 open the 0x73 gate; bits[2:0]=000 PCLK /1)
      0x72, // DCWCTR       expect 0x00 (no down sampling; default 0x11 = HDS by 2, Table 6-2)
      0x73, // PCLK_DIV     expect 0x08 (bit[3]=1 bypass divider, matches RGB565)
      0x74, // REG74        expect 0x20 (Horizontal Scaling Ratio 1x, Table 6-1; matches RGB565)
      0x75, // REG75        expect 0x0F (not written by init; reset default)
      0xA2, // PCLK_DELAY   expect 0x02 (Table 2-2 VGA raw ref)
#else
      0x0A, // PID         expect 0x76 (OV7670)
      0x0B, // VER         expect 0x73
      0x12, // COM7        expect 0x14 (QVGA + RGB)
      0x40, // COM15       expect 0xD0 (RGB565 full range)
      0x15, // COM10       expect 0x02
      0x11, // CLKRC       expect 0x80 (PWM XCLK) / 0x01 (PIO 8 MHz)
      0x6B, // DBLV        expect 0x0A (PLL x2)
      0x1E, // MVFP        expect 0x07 (no flip)
      0x13, // COM8        expect 0xE7 (AGC+AEC+AWB on)
      0x14, // COM9        not written by init; live readback 0x4A (reset default)
      0x00, // GAIN        AGC current gain (live)
      0x10, // AECH        exposure high byte (live)
      0x24, // AEW         AGC lower limit
      0x25, // AEB         AGC upper limit
      0x32, // HREF        HREF start/end
      0x70, // SCALING_XSC
      0x71, // SCALING_YSC
#endif
  };
  Serial.write(DBG_MAGIC0);
  Serial.write(DBG_MAGIC1);
  Serial.write(DBG_MAGIC2);
  Serial.write(DBG_MAGIC3);
  Serial.write(REG_MARKER);
  Serial.write((uint8_t)sizeof(regs));
  for (uint8_t i = 0; i < sizeof(regs); i++) {
    Serial.write(regs[i]);
    Serial.write(read_reg_checked(regs[i]));
  }
}

// ---- Waveform snapshot (diagnostic) ----
// Host sends 'W' (fast) or 'S' (slow) over USB; MCU replies with
//   DBG1 + 0xFC + type(0x01|0x02) + count(2 BE) + dur_us(4 BE) + samples
// Samples are packed 2/byte (high nibble = sample i, low = sample i+1;
// bit3=VSYNC bit2=HREF bit1=PCLK bit0=D0). dur_us is the measured capture
// window so the host can turn sample counts into real time.
// 'W': tight loop, no delay, ~170ns/sample, 16384 samples (~2.8ms): resolves
//   HREF pulse shape within a line and PCLK/D0 behaviour while HREF is high.
// 'S': 100us interval, 400 samples (40ms ~ 1.2 frames): counts HREF pulses
//   per frame and locates VSYNC. The 1ms/10us probes alias HREF badly, so
//   the raw snapshot is ground truth for polarity and duty.
#define WAVE_MARKER 0xFC
#define WAVE_TYPE_FAST 0x01
#define WAVE_TYPE_SLOW 0x02
#define WAVE_FAST_SAMPLES 16384
#define WAVE_SLOW_SAMPLES 400
#define WAVE_SLOW_INTERVAL_US 100

static uint8_t wave_buf[WAVE_FAST_SAMPLES / 2];

static void wave_send_packet(uint8_t type, uint16_t count, uint32_t dur_us) {
  Serial.write(DBG_MAGIC0);
  Serial.write(DBG_MAGIC1);
  Serial.write(DBG_MAGIC2);
  Serial.write(DBG_MAGIC3);
  Serial.write(WAVE_MARKER);
  Serial.write(type);
  Serial.write((uint8_t)(count >> 8));
  Serial.write((uint8_t)(count & 0xFF));
  Serial.write((uint8_t)(dur_us >> 24));
  Serial.write((uint8_t)(dur_us >> 16));
  Serial.write((uint8_t)(dur_us >> 8));
  Serial.write((uint8_t)(dur_us & 0xFF));
  Serial.write(wave_buf, (count + 1) / 2);
}

static void wave_capture_fast(void) {
  uint32_t t0 = time_us_32();
  uint8_t *p = wave_buf;
  for (uint32_t i = 0; i < WAVE_FAST_SAMPLES; i += 2) {
    // Volatile SIO reads can't be reordered/eliminated; two samples per
    // iteration keep the loop overhead (and thus sample jitter) low.
    uint8_t s0 = (gpio_get(PIN_VSYNC) << 3) | (gpio_get(PIN_HREF) << 2) |
                 (gpio_get(PIN_PCLK) << 1) | gpio_get(PIN_D0);
    uint8_t s1 = (gpio_get(PIN_VSYNC) << 3) | (gpio_get(PIN_HREF) << 2) |
                 (gpio_get(PIN_PCLK) << 1) | gpio_get(PIN_D0);
    *p++ = (uint8_t)((s0 << 4) | s1);
  }
  wave_send_packet(WAVE_TYPE_FAST, WAVE_FAST_SAMPLES, time_us_32() - t0);
}

static void wave_capture_slow(void) {
  uint32_t t0 = time_us_32();
  uint8_t *p = wave_buf;
  for (uint32_t i = 0; i < WAVE_SLOW_SAMPLES; i++) {
    uint8_t s = (gpio_get(PIN_VSYNC) << 3) | (gpio_get(PIN_HREF) << 2) |
                (gpio_get(PIN_PCLK) << 1) | gpio_get(PIN_D0);
    if (i & 1) {
      *p++ |= s; // odd sample -> low nibble of the current byte
    } else {
      *p = (uint8_t)(s << 4); // even sample -> high nibble of a fresh byte
    }
    busy_wait_us(WAVE_SLOW_INTERVAL_US);
  }
  wave_send_packet(WAVE_TYPE_SLOW, WAVE_SLOW_SAMPLES, time_us_32() - t0);
}

// ---- Per-HREF-line PCLK edge counter (diagnostic 'C' command) ----
// Decisive T7 measurement: how many PCLK rising edges fall inside one HREF
// line? 640 (official Table 3-3 raw timing) or 1280 (each distinct byte held
// 2 PCLKs)? Runs pclk_count on SM2 while the capture SM1/DMA keep streaming:
// SM2 pushes one 32-bit word per line = the remaining countdown from
// 0xFFFFFFFF, so the host computes edges = 0xFFFFFFFF - value:
//   640 edges/line  -> 0xFFFFFF80
//   1280 edges/line -> 0xFFFFFF00
// Reply: DBG1 + 0xF8 + nlines + nlines * (u32 BE remaining count).
// nlines = 0 means no HREF line was seen within the timeout (camera dead).
// SM clock stays at 125 MHz (clkdiv 1): the count loop is ~80 ns minimum per
// edge, so it resolves the 10.4 MHz (96 ns) and 5.2 MHz (192 ns) hypotheses.
#define COUNT_MARKER 0xF8
#define COUNT_LINES 4

static void pclk_count_send(void) {
  static uint cnt_off = 0; // pclk_count program offset (loaded once)
  if (cnt_off == 0) {
    cnt_off = pio_add_program(CAP_PIO, &pclk_count_program);
  }
  pio_sm_config c = pclk_count_program_get_default_config(cnt_off);
  // jmp pin must track HREF: absolute GPIO 17, NOT the IN base (8)
  sm_config_set_jmp_pin(&c, PIN_HREF);
  pio_sm_init(CAP_PIO, SM_COUNT, cnt_off, &c);
  pio_sm_clear_fifos(CAP_PIO, SM_COUNT);
  pio_sm_set_enabled(CAP_PIO, SM_COUNT, true);

  uint32_t vals[COUNT_LINES];
  uint32_t got = 0;
  uint32_t t0 = millis();
  while (got < COUNT_LINES && millis() - t0 < 500) {
    if (!pio_sm_is_rx_fifo_empty(CAP_PIO, SM_COUNT)) {
      vals[got++] = pio_sm_get(CAP_PIO, SM_COUNT);
    }
  }
  pio_sm_set_enabled(CAP_PIO, SM_COUNT, false);

  Serial.write(DBG_MAGIC0);
  Serial.write(DBG_MAGIC1);
  Serial.write(DBG_MAGIC2);
  Serial.write(DBG_MAGIC3);
  Serial.write(COUNT_MARKER);
  Serial.write((uint8_t)got);
  for (uint32_t i = 0; i < got; i++) {
    Serial.write((uint8_t)(vals[i] >> 24));
    Serial.write((uint8_t)(vals[i] >> 16));
    Serial.write((uint8_t)(vals[i] >> 8));
    Serial.write((uint8_t)(vals[i] & 0xFF));
  }
}

// ---- HREF line counter (diagnostic 'H') ----
// Counts HREF lines over a 1s window on SM2 (href_count program: X decrements
// once per complete HREF line). Reply: DBG1 + 0xF6 + lines(4 BE) +
// vsync_delta(4 BE). Host: lines_per_frame = lines / vsync_delta.
// vsync_delta = RAW vsync edge count (vsync_irq_count), not the armed count:
// the 1s window busy-waits, so loop() never consumes frames and the DMA
// arming guard (!frame_ready && !dma_busy) holds -> only ~1 edge would arm.
// During multi-fire, spurious edges inflate vsync_delta and lines/frame reads
// LOW (a vertical-timing-instability signal); the ring (bit7) independently
// proves those edges never arm mid-window, so no rotation. Discriminator:
// 240 lines/frame = half-window active; 477 = full VGA window (fresh init,
// no 'T' yet). The 1s blocking window drops a few streaming frames on USB;
// host sends 'H' standalone.
#define HREF_COUNT_MARKER 0xF6
#define HREF_COUNT_WINDOW_MS 1000

static void href_count_send(void) {
  static uint href_off = 0; // href_count program offset (loaded once)
  if (href_off == 0) {
    href_off = pio_add_program(CAP_PIO, &href_count_program);
  }
  pio_sm_config c = href_count_program_get_default_config(href_off);
  pio_sm_init(CAP_PIO, SM_COUNT, href_off, &c);
  pio_sm_clear_fifos(CAP_PIO, SM_COUNT);
  pio_sm_set_enabled(CAP_PIO, SM_COUNT, true);

  uint32_t vsync_0 = vsync_irq_count;
  uint32_t t0 = millis();
  while (millis() - t0 < HREF_COUNT_WINDOW_MS) {
  }
  pio_sm_set_enabled(CAP_PIO, SM_COUNT, false);

  // Read X via single-step: in x,32 -> ISR; push ISR -> FIFO; pop FIFO.
  pio_sm_exec(CAP_PIO, SM_COUNT, pio_encode_in(pio_x, 32));
  pio_sm_exec(CAP_PIO, SM_COUNT, pio_encode_push(false, false));
  uint32_t x = pio_sm_get_blocking(CAP_PIO, SM_COUNT);
  uint32_t lines = 0xFFFFFFFFu - x;
  uint32_t vsync_delta = vsync_irq_count - vsync_0;

  Serial.write(DBG_MAGIC0);
  Serial.write(DBG_MAGIC1);
  Serial.write(DBG_MAGIC2);
  Serial.write(DBG_MAGIC3);
  Serial.write(HREF_COUNT_MARKER);
  Serial.write((uint8_t)(lines >> 24));
  Serial.write((uint8_t)(lines >> 16));
  Serial.write((uint8_t)(lines >> 8));
  Serial.write((uint8_t)(lines & 0xFF));
  Serial.write((uint8_t)(vsync_delta >> 24));
  Serial.write((uint8_t)(vsync_delta >> 16));
  Serial.write((uint8_t)(vsync_delta >> 8));
  Serial.write((uint8_t)(vsync_delta & 0xFF));
}

static void capture_pio_setup(void) {
  // Pixel capture on GP8..GP15
#ifdef RAW_BAYER_PER_PCLK
  // Per-PCLK sampling (official Table 3-3 raw timing model: 1 byte/PCLK).
  // Used by the official-config A/B (8th group): if the official Table 2-2
  // registers make the sensor emit one distinct byte per PCLK, dup_even
  // drops without the every-2nd-PCLK workaround. Shipped build never loads
  // this program (capture above is used instead).
  cap_off = pio_add_program(CAP_PIO, &capture_per_pclk_program);
  capture_per_pclk_program_init(CAP_PIO, SM_CAPTURE, cap_off, PIN_D0);
#else
  // Every-2nd-PCLK sampling (T7 workaround, shipped build).
  cap_off = pio_add_program(CAP_PIO, &capture_program);
  capture_program_init(CAP_PIO, SM_CAPTURE, cap_off, PIN_D0);
#endif

  // VSYNC as GPIO interrupt (PIO waits on absolute GPIO 17/18 for
  // HREF/PCLK; VSYNC is handled by GPIO IRQ for frame timing). HREF gets a
  // falling-edge IRQ too: vsync_isr uses it to measure HREF-low duration so
  // the DMA only re-arms inside the vblank (see VBLANK_GATE_US above).
  gpio_init(PIN_VSYNC);
  gpio_set_dir(PIN_VSYNC, GPIO_IN);
  gpio_pull_down(PIN_VSYNC);
  gpio_init(PIN_HREF);
  gpio_set_dir(PIN_HREF, GPIO_IN);
  gpio_set_irq_enabled_with_callback(PIN_VSYNC, GPIO_IRQ_EDGE_RISE, true,
                                     vsync_isr);
  gpio_set_irq_enabled(PIN_HREF, GPIO_IRQ_EDGE_FALL, true);
}

void setup() {
  Serial.begin(115200); // USB CDC

  // Boot banner: identifies which firmware is running (impl/uvc vs main).
  // Same code on both branches - USE_TINYUSB picks the string at compile time.
  Serial.println();
#ifdef USE_TINYUSB
  Serial.println("FW: impl/uvc (TinyUSB UVC)");
#else
  Serial.println("FW: main (PIO+DMA CDC)");
#endif
#ifdef RAW_BAYER
  Serial.println("Mode: raw bayer 320x240 (CAM2)");
#endif
  Serial.print("Build: ");
  Serial.print(__DATE__);
  Serial.print(" ");
  Serial.println(__TIME__);

  // Give host a moment to enumerate USB before we start the stream
  delay(1000);

  // Start XCLK BEFORE SCCB detect: some OV7670 modules do not answer on
  // SCCB until the pixel clock is running (matches reference implementation).
  xclk_start();

  if (!ov7670_detect()) {
    uint8_t pid = 0;
    uint8_t status;
    if (ov7670_read_reg(OV7670_REG_PID, &pid) != 0) {
      status = ov7670_i2c_scan() ? 0xF2 : 0xFF;
    } else {
      status = 0xF1;
    }
    while (true) {
      Serial.write(DBG_MAGIC0);
      Serial.write(DBG_MAGIC1);
      Serial.write(DBG_MAGIC2);
      Serial.write(DBG_MAGIC3);
      Serial.write(status); // 0xFF=bus dead 0xF2=other addr ACKs 0xF1=wrong PID
      Serial.write(ov7670_bus_levels()); // bit1=SCL, bit0=SDA idle level
      Serial.write(xclk_activity_probe()); // high count 0..100, ~50 = toggling
      delay(500); // retry so host sees it whenever it opens the port
    }
  }

#ifdef RAW_BAYER
  ov7670_init_raw_bayer();
  ov7670_set_bayer_window_320x240();
#else
  ov7670_init();
#endif
  cam_ready = true;
  capture_pio_setup();
  dma_setup();
  last_frame_ms = millis();
}

void loop() {
  // Diagnostic waveform commands ('W' fast / 'S' slow snapshot). Direct GPIO
  // reads only: the PIO/DMA capture pipeline keeps running untouched, so a
  // snapshot never disturbs the camera stream. Drain all pending bytes so a
  // quick 'WS' from the host runs both captures back to back.
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c == 'W') {
      wave_capture_fast();
    } else if (c == 'S') {
      wave_capture_slow();
    } else if (c == 'R') {
      // Register readback: proves the init writes landed and shows live
      // AGC/AEC state (see reg_readback_send). Reply is DBG1 + 0xFB, so the
      // host frame-sync loop passes it through like every other diagnostic.
      reg_readback_send();
    } else if (c == 'C') {
      // Per-HREF-line PCLK edge counter (SM2): reply DBG1 + 0xF8 + n +
      // n * (u32 BE remaining countdown). Host: edges = 0xFFFFFFFF - val.
      // Settles 640 vs 1280 PCLK/line (T7) without touching the capture path.
      pclk_count_send();
    } else if (c == 'H') {
      // HREF line counter (SM2): reply DBG1 + 0xF6 + lines(4 BE) +
      // vsync_delta(4 BE) over a 1s window. Host: lines/frame = lines/delta
      // (240 = half-window active, 477 = full window, varying = instability).
      href_count_send();
    } else if (c == 'V') {
      // VSYNC-edge ring dump (see vsync_isr for the byte layout):
      // reply DBG1 + 0xF7 + idx(4 BE) + VSYNC_RING_LEN * (state + ts u16 BE).
      Serial.write(DBG_MAGIC0);
      Serial.write(DBG_MAGIC1);
      Serial.write(DBG_MAGIC2);
      Serial.write(DBG_MAGIC3);
      Serial.write((uint8_t)0xF7);
      uint32_t ri = vsync_ring_i;
      Serial.write((uint8_t)(ri >> 24));
      Serial.write((uint8_t)(ri >> 16));
      Serial.write((uint8_t)(ri >> 8));
      Serial.write((uint8_t)(ri & 0xFF));
      for (uint32_t i = 0; i < VSYNC_RING_LEN; i++) {
        Serial.write(vsync_ring[i]);
        uint16_t t = vsync_ring_ts[i];
        Serial.write((uint8_t)(t >> 8));
        Serial.write((uint8_t)(t & 0xFF));
      }
    } else if (c == 'B') {
      // Software reboot into BOOTSEL: host sends 'B' and the Pico re-enumerates
      // as the RPI-RP2 mass-storage drive for drag-and-drop flashing (no
      // physical button press needed on flash-test-fix iterations).
      reset_usb_boot(0, 0);
    }
  }

  if (!frame_ready) {
    if (cam_ready && millis() - last_frame_ms > 3000) {
      static uint32_t last_report = 0;
      if (millis() - last_report > 500) {
        Serial.write(DBG_MAGIC0);
        Serial.write(DBG_MAGIC1);
        Serial.write(DBG_MAGIC2);
        Serial.write(DBG_MAGIC3);
        Serial.write((uint8_t)0xFD); // status marker: no VSYNC frame
        Serial.write(gpio_activity_probe(PIN_VSYNC)); // 0/100 stuck, ~5 pulse
        Serial.write(gpio_activity_probe(PIN_HREF)); // 0=stuck low, ~15 pulse
        Serial.write(gpio_activity_probe(PIN_PCLK)); // ~50 pulse, 0/100 stuck
        Serial.write(gpio_activity_probe(PIN_D0)); // ~50 = pixel data flowing
        Serial.write(gpio_edge_probe(PIN_VSYNC)); // 0=dead, 2-20=frame timing
        Serial.write(gpio_edge_probe(PIN_HREF)); // 0=dead, 255=lines flowing
        // Register read-back: prove the init writes landed (a camera that
        // drops writes runs its default state: stuck VSYNC, no HREF).
        Serial.write(read_reg_checked(0x12)); // COM7 expect 0x14 (QVGA+RGB) / 0x01 (raw)
        Serial.write(read_reg_checked(0x40)); // COM15 expect 0xC0 (full range, no RGB565 — fixes D7=D0)
        Serial.write(read_reg_checked(0x15)); // COM10 expect 0x02 / live
        Serial.write(read_reg_checked(0x11)); // CLKRC expect 0x80 (PWM) / 0x01 (raw)
        Serial.write(read_reg_checked(0x1e)); // MVFP expect 0x07 (no flip)
        Serial.write(read_reg_checked(0x6b)); // DBLV expect 0x0A (PLL x2)
        // On-chip state: where the pipeline is actually stuck.
        // SM PC (0..3) via pc-cap_off; FIFO 0..8 words (8 = SM stalled on
        // autopush); DMA remaining (0 = completed, 153600 = never started);
        // ctrl_trig full: bit0 BUSY, [3:2] DATA_SIZE, bit4/5 INCR_R/W,
        // [20:15] TREQ_SEL (5 = PIO0 RX1, 0x1F = FORCE);
        // dma_chan signed: 0xFF = -1 = dma_setup() never completed (the
        //   cam_ready=true marker is set BEFORE dma_setup() runs, so a
        //   panic there leaves dma_chan=-1 and every ch[] read is garbage);
        // vsync_irq_count: 0 = edge IRQ never fires -> DMA never armed.
        Serial.write((uint8_t)(pio_sm_get_pc(CAP_PIO, SM_CAPTURE) - cap_off));
        Serial.write((uint8_t)pio_sm_get_rx_fifo_level(CAP_PIO, SM_CAPTURE));
        uint32_t dma_remain = dma_hw->ch[dma_chan].al3_transfer_count;
        Serial.write((uint8_t)(dma_remain >> 24));
        Serial.write((uint8_t)(dma_remain >> 16));
        Serial.write((uint8_t)(dma_remain >> 8));
        Serial.write((uint8_t)(dma_remain & 0xFF));
        uint32_t dma_ctrl = dma_hw->ch[dma_chan].ctrl_trig;
        Serial.write((uint8_t)(dma_ctrl >> 24));
        Serial.write((uint8_t)(dma_ctrl >> 16));
        Serial.write((uint8_t)(dma_ctrl >> 8));
        Serial.write((uint8_t)(dma_ctrl & 0xFF));
        Serial.write((uint8_t)(int8_t)dma_chan); // 0xFF = -1: setup never ran
        uint32_t vc = vsync_irq_count;
        Serial.write((uint8_t)(vc >> 8));
        Serial.write((uint8_t)(vc & 0xFF));
        uint32_t dc = dma_done_count;
        Serial.write((uint8_t)(dc >> 8));
        Serial.write((uint8_t)(dc & 0xFF));
        last_report = millis();
      }
    }
    return;
  }

  // Frame header: "CAM1"/"CAM2" + W (2 BE) + H (2 BE) - matches capture.py
  Serial.write(FRAME_MAGIC0);
  Serial.write(FRAME_MAGIC1);
  Serial.write(FRAME_MAGIC2);
  Serial.write(FRAME_MAGIC3);
  Serial.write((uint8_t)(FRAME_W >> 8));
  Serial.write((uint8_t)(FRAME_W & 0xFF));
  Serial.write((uint8_t)(FRAME_H >> 8));
  Serial.write((uint8_t)(FRAME_H & 0xFF));

  // Raw RGB565 payload. Serial.write blocks until buffered. The flag is
  // cleared only AFTER the stream finishes so a VSYNC IRQ during the send
  // sees frame_ready==true and drops its start request instead of writing
  // over the buffer being read. The next VSYNC then starts the next frame.
  Serial.write(frames, FRAME_BYTES);
  frame_ready = false;
  last_frame_ms = millis();
}
