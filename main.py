"""
TAMAGOTCHI PLAYER (V8 - Multi-state) - ESP32 PORT

Animations:
  character_anim.bin -> default loop (1..9 files on USB)
  eating_anim.bin      -> one-shot when a file is added
  crying_anim.bin      -> one-shot, then get-up, when a file is removed
  get-up_anim.bin      -> one-shot after crying (chained)
  hangry_anim.bin      -> loop when 0 files on USB, or one-shot on 5+ delete streak (see a)
  grow_anim.bin        -> one-shot when first crossing to 10+ files, then -> chonk
  chonk_anim.bin       -> loop when 10+ files on USB

Protocol (single chars over USB serial from watcher.py):
  e = eat (transient)     i = base idle
  s = sad then get-up     h = base hangry (0 files)
  a = hangry one-shot     c = base chonk
      (5+ delete streak)  g = grow then chonk
OFFSET CONFIG:
  BG_Y_OFFSET   negative = animation up,  positive = animation down
  BAR_Y_OFFSET  negative = bar up,        positive = bar down

--------------------------------------------------------------------


ESP32 WIRING for a 1.44" 128x128 ST7735 module (8-pin: GND, VCC,
SCL/SCK, SDA/DIN/MOSI, RES/RST, DC/A0, CS, BLK/LED):
  Display pin      | ESP32 GPIO | Notes
  -----------------|------------|---------------------------------
  VCC              | 3.3V       | Do NOT use 5V on a 3.3V panel
  GND              | GND        |
  SCL / SCK        | GPIO18     | VSPI clock
  SDA / DIN / MOSI | GPIO23     | VSPI data out
  RES / RST        | GPIO26     |
  DC / A0          | GPIO27     |
  CS               | GPIO5      | strapping pin -- see note below
  BLK / LED        | 3.3V       | backlight -- tie straight to
                    |            | 3.3V if you don't need dimming

Note on GPIO5: it's an ESP32 boot-strapping pin (must read HIGH
during reset). A display CS line floating/pulled high is normally
fine, but if you see random boot weirdness, move CS to another
free GPIO (e.g. 25) and update CS_PIN below.

--------------------------------------------------------------------
EXTRA HARDWARE (gyro, LED, servo, button) -- none of these pins
overlap the display pins above:
  MPU6050 SDA      | GPIO21     | I2C data
  MPU6050 SCL      | GPIO22     | I2C clock
  LED (+resistor)  | GPIO4      | plain LED, PWM brightness/blink
  Servo signal     | GPIO13     | 50Hz hobby servo. Power the servo
                    |            | from a SEPARATE 5V supply, not the
                    |            | ESP32's own 3.3V/5V pin -- share
                    |            | ground only.
  Push button      | GPIO14     | other leg to GND, internal pull-up
                    |            | (short press = interact, long
                    |            | press 3s = sleep toggle)
--------------------------------------------------------------------
"""

from machine import Pin, SPI, I2C, PWM
import time
import uos
import sys
import uselect


# --- CONFIGURATION ---
WIDTH      = 128
HEIGHT     = 128
BYTES_PP   = 2
ROW_BYTES  = WIDTH * BYTES_PP
FRAME_SIZE = WIDTH * HEIGHT * BYTES_PP   # 32768 bytes

BAR_HEIGHT = 24
BAR_SIZE   = WIDTH * BAR_HEIGHT * BYTES_PP  # 6144 bytes

# -------------------------------------------------------
# ST7735 PANEL RAM OFFSET
# The ST7735's internal display RAM is larger than the visible
# 128x128 glass on most of these boards, so the visible area sits
# at an offset inside it. Common values are (2, 1), (2, 3), or
# (0, 0) depending on the specific panel batch ("green tab" /
# "red tab" boards vary). Start with these; if you see a colored
# line on one edge, or the image is shifted, try (2,1) or (0,0).
PANEL_X_OFFSET = 2
PANEL_Y_OFFSET = 3
# -------------------------------------------------------

# -------------------------------------------------------
# OFFSETS
BG_Y_OFFSET  = -10   # negative = move animation up, positive = move down
BAR_Y_OFFSET = 2   # negative = move bar up, positive = move bar down
# -------------------------------------------------------

# -------------------------------------------------------

RECOLOR_STYLE = None

# --- 'pink_body' settings ---
PINK_COLOR    = (255, 105, 180)
LEAF_COLOR    = (70, 200, 90)
BG_LUM_THRESH = 15     # pixels darker than this stay pure black

# --- 'tint' settings (only used if RECOLOR_STYLE == 'tint') ---
COLOR_INVERT      = False             # True = photo-negative
COLOR_TINT        = None
COLOR_TINT_AMOUNT = 0.30
COLOR_SWAP_RB     = False
# Examples:
#   COLOR_TINT = (255, 102, 170);  COLOR_TINT_AMOUNT = 0.35   # pink wash
#   COLOR_TINT = (80,  180, 255);  COLOR_TINT_AMOUNT = 0.25   # cool/blue
#   COLOR_TINT = (255, 200, 80);   COLOR_TINT_AMOUNT = 0.30   # warm/sunset
# -------------------------------------------------------

TARGET_FPS = 8
FRAME_MS   = int(1000 / TARGET_FPS)

# --- ESP32 PIN ASSIGNMENT (see wiring table in the module docstring) ---
SCK_PIN = 18
MOSI_PIN = 23
CS_PIN  = 5
RST_PIN = 26
DC_PIN  = 27

# SPI Setup (VSPI = SPI id 2 on ESP32). Write-only device, so we
# don't wire/declare a MISO pin.
# ST7735 boards are commonly reliable up to ~15-20MHz on breadboard
# wiring; drop to 10000000 if you see flickering/garbled frames.
spi = SPI(2, baudrate=20000000, polarity=0, phase=0,
          sck=Pin(SCK_PIN), mosi=Pin(MOSI_PIN))
dc  = Pin(DC_PIN, Pin.OUT)
cs  = Pin(CS_PIN, Pin.OUT)
rst = Pin(RST_PIN, Pin.OUT)

# -------------------------------------------------------
# EXTRA HARDWARE: gyro (MPU6050), LED, servo, push button
# All on pins not used by the display above (18/23/5/26/27).
# -------------------------------------------------------
I2C_SDA_PIN = 21
I2C_SCL_PIN = 22
LED_PIN     = 4
SERVO_PIN   = 13
BUTTON_PIN  = 14

# MPU6050 (accelerometer) over I2C
i2c = I2C(0, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN), freq=400000)
MPU_ADDR = 0x68


_MPU_OK = False
try:
    i2c.writeto_mem(MPU_ADDR, 0x6B, b'\x00')
    _MPU_OK = True
    print("D: MPU6050 gyro found, shake detection enabled")
except OSError as e:
    print("Warning: MPU6050 not responding ({}), shake detection disabled".format(e))

SHAKE_THRESHOLD = 12000   # tune against your own readings if shakes
                          # aren't triggering, or trigger too easily
_last_accel_mag = 0

# Plain LED (not addressable) on PWM for brightness/blink patterns
led = PWM(Pin(LED_PIN, Pin.OUT), freq=1000)
led.duty_u16(0)
# NOTE: if your firmware's PWM object has no duty_u16 (older
# MicroPython), switch every led.duty_u16(x) below to
# led.duty(x * 1023 // 65535) instead.

# Servo on PWM (standard 50Hz hobby servo signal)
servo = PWM(Pin(SERVO_PIN), freq=50)
_servo_angle  = 90.0
_servo_target = 90.0
SERVO_STEP = 4   # degrees per frame -- raise for snappier, lower for smoother

# Push button: short press = interact/"pet", long press (3s) = sleep toggle.
# Wired to GND with internal pull-up, so idle = 1, pressed = 0.
button = Pin(BUTTON_PIN, Pin.IN, Pin.PULL_UP)
_press_start = None
LONG_PRESS_MS = 3000
asleep = False

# --- HELPER FUNCTIONS ---
def cmd(c):
    dc.value(0)
    cs.value(0)
    spi.write(bytes([c]))
    cs.value(1)

def data(buf):
    dc.value(1)
    cs.value(0)
    spi.write(buf)
    cs.value(1)

def set_window():
    """ST7735 CASET/RASET/RAMWR -- select the full 128x128 window
    (shifted by PANEL_X_OFFSET/PANEL_Y_OFFSET) and prep for a
    pixel-data write.

    IMPORTANT: unlike cmd()/data(), this leaves CS asserted (low)
    and DC high when it returns -- the main loop immediately follows
    this with spi.write(mv_frame) as part of the SAME transaction,
    then raises CS itself. Do not use the cmd()/data() helpers here,
    they each release CS after a single call."""
    x0 = PANEL_X_OFFSET
    x1 = PANEL_X_OFFSET + WIDTH - 1
    y0 = PANEL_Y_OFFSET
    y1 = PANEL_Y_OFFSET + HEIGHT - 1

    cs.value(0)
    dc.value(0)
    spi.write(b'\x2A')                  # CASET - column address set
    dc.value(1)
    spi.write(bytes([0x00, x0, 0x00, x1]))

    dc.value(0)
    spi.write(b'\x2B')                  # RASET - row address set
    dc.value(1)
    spi.write(bytes([0x00, y0, 0x00, y1]))

    dc.value(0)
    spi.write(b'\x2C')                  # RAMWR - memory write
    dc.value(1)                         # pixel data follows; CS stays low

def hard_reset():
    cs.value(1)
    rst.value(1)
    time.sleep(0.1)
    rst.value(0)
    time.sleep(0.2)
    rst.value(1)
    time.sleep(0.2)

def init_display():
    """Standard ST7735 init sequence (works for the common 1.44"
    128x128 'green tab' / 'red tab' boards). If colors look swapped
    (red<->blue) try changing the MADCTL value below from 0xC0 to
    0xC8 (toggles the BGR bit)."""
    hard_reset()

    cmd(0x01)                 # SWRESET - software reset
    time.sleep(0.15)

    cmd(0x11)                 # SLPOUT - sleep out / booster on
    time.sleep(0.15)

    cmd(0xB1); data(bytes([0x01, 0x2C, 0x2D]))   # FRMCTR1 - frame rate (normal mode)
    cmd(0xB2); data(bytes([0x01, 0x2C, 0x2D]))   # FRMCTR2 - frame rate (idle mode)
    cmd(0xB3); data(bytes([0x01, 0x2C, 0x2D,
                            0x01, 0x2C, 0x2D]))  # FRMCTR3 - frame rate (partial mode)

    cmd(0xB4); data(b'\x07')              # INVCTR - display inversion control

    cmd(0xC0); data(bytes([0xA2, 0x02, 0x84]))   # PWCTR1 - power control 1
    cmd(0xC1); data(b'\xC5')                     # PWCTR2 - power control 2
    cmd(0xC2); data(bytes([0x0A, 0x00]))         # PWCTR3 - power control 3
    cmd(0xC3); data(bytes([0x8A, 0x2A]))         # PWCTR4 - power control 4
    cmd(0xC4); data(bytes([0x8A, 0xEE]))         # PWCTR5 - power control 5

    cmd(0xC5); data(b'\x0E')              # VMCTR1 - VCOM control

    cmd(0x20)                 # INVOFF - display inversion off

    cmd(0x36); data(b'\xC0')              # MADCTL - memory access control
                                           # (orientation/RGB order; try 0xC8 if
                                           # colors are swapped)

    cmd(0x3A); data(b'\x05')              # COLMOD - 16-bit/pixel (RGB565)

    # Gamma correction (standard ST7735 values)
    cmd(0xE0); data(bytes([0x02, 0x1C, 0x07, 0x12, 0x37, 0x32, 0x29, 0x2D,
                            0x29, 0x25, 0x2B, 0x39, 0x00, 0x01, 0x03, 0x10]))
    cmd(0xE1); data(bytes([0x03, 0x1D, 0x07, 0x06, 0x2E, 0x2C, 0x29, 0x2D,
                            0x2E, 0x2E, 0x37, 0x3F, 0x00, 0x00, 0x02, 0x10]))

    cmd(0x13)                 # NORON - normal display mode on
    time.sleep(0.01)

    cmd(0x29)                 # DISPON - display on
    time.sleep(0.05)

def clear_uncovered_band():
    """Clear only the strip of rows that BG_Y_OFFSET leaves uncovered
    by the animation each frame -- NOT the whole 32KB frame.

    Replaces the old clear_framebuf()/BLACK_FRAME approach, which kept
    a permanent extra 32KB buffer around just to blank the screen.
    That's 96KB resident (framebuf + anim_buf + BLACK_FRAME) on a chip
    that typically only has ~100-115KB free -- easy to tip over into
    a MemoryError, especially once anything else (WiFi stack, other
    files loaded at boot) eats into that headroom too.

    With BG_Y_OFFSET=-10 (the default), this clears only 10 rows
    (2560 bytes) instead of all 128 rows (32768 bytes) -- cheaper
    AND removes a permanent 32KB allocation entirely."""
    if BG_Y_OFFSET == 0:
        return  # animation fully covers the frame; nothing to clear
    uncovered = abs(BG_Y_OFFSET)
    if BG_Y_OFFSET < 0:
        start_row = HEIGHT - uncovered   # content shifted up -> bottom band is bare
    else:
        start_row = 0                    # content shifted down -> top band is bare
    start_byte = start_row * ROW_BYTES
    for r in range(uncovered):
        off = start_byte + r * ROW_BYTES
        mv_frame[off:off + ROW_BYTES] = _ZERO_ROW

def composite_bg_with_offset():
    """
    Copy anim_buf into framebuf with vertical offset.
    Negative BG_Y_OFFSET = move animation up
    Positive BG_Y_OFFSET = move animation down
    """
    src_row_start = max(0, -BG_Y_OFFSET)
    dst_row_start = max(0,  BG_Y_OFFSET)

    rows = min(HEIGHT - src_row_start, HEIGHT - dst_row_start)
    if rows <= 0:
        return

    for r in range(rows):
        src = (src_row_start + r) * ROW_BYTES
        dst = (dst_row_start + r) * ROW_BYTES
        mv_frame[dst:dst + ROW_BYTES] = mv_anim[src:src + ROW_BYTES]

def _pack565(r, g, b):
    """Pack an 8-bit RGB color into 2 big-endian RGB565 bytes."""
    val = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return bytes([(val >> 8) & 0xFF, val & 0xFF])

# --- MOOD BAR (drawn procedurally, no bar.bin file needed) ---
# One color + fill level per base state -- matches the pink/black/
# green palette already baked into the animations. Tweak freely.
BAR_COLORS = {
    'hangry': (220, 90, 60),     # warm red/orange -- low, urgent
    'idle':   (255, 105, 180),   # pink -- matches the body color
    'chonk':  (70, 200, 90),     # green -- matches the leaf, "full"
}
BAR_FILL_PCT = {
    'hangry': 0.20,
    'idle':   0.60,
    'chonk':  1.00,
}
BAR_BORDER_COLOR = (70, 70, 70)   # dim grey outline so it reads as
                                   # a container even when nearly empty
BAR_BORDER_PX = 1

def composite_bar():
    """Draw a live mood bar into the reserved band at the bottom of
    the screen. Color and fill level follow base_state -- this is
    computed fresh every frame (cheap: it's just row-length byte
    fills, not a per-pixel loop), so it's always in sync with
    whatever's currently happening, no separate art file required."""
    if rows_visible <= 0:
        return

    color = BAR_COLORS.get(base_state, (255, 105, 180))
    pct   = BAR_FILL_PCT.get(base_state, 0.5)

    fill_px   = _pack565(*color)
    border_px = _pack565(*BAR_BORDER_COLOR)
    black_px  = _pack565(0, 0, 0)

    inner_width = WIDTH - 2 * BAR_BORDER_PX
    fill_width  = int(inner_width * pct)
    black_width = inner_width - fill_width

    border_row = border_px * WIDTH
    fill_row   = border_px + fill_px * fill_width + black_px * black_width + border_px

    for r in range(rows_visible):
        row_off = bar_byte_start + r * ROW_BYTES
        is_border_row = (r < BAR_BORDER_PX) or (r >= rows_visible - BAR_BORDER_PX)
        mv_frame[row_off:row_off + ROW_BYTES] = border_row if is_border_row else fill_row


# --- RUNTIME COLOR TRANSFORMS (plain Python -- ESP32 has no viper) ---
# All operate IN PLACE on a buffer of big-endian RGB565 bytes.
# Pixel layout in memory: byte0 = RRRRRGGG, byte1 = GGGBBBBB.
# Only called if COLOR_INVERT / COLOR_SWAP_RB / COLOR_TINT is enabled
# above -- off by default.

def _invert_buf(buf, nbytes):
    for i in range(nbytes):
        buf[i] = 0xFF ^ buf[i]

def _swap_rb_buf(buf, nbytes):
    i = 0
    while i < nbytes:
        b0 = buf[i]
        b1 = buf[i + 1]
        r5 = (b0 >> 3) & 0x1F
        b5 = b1 & 0x1F
        buf[i]     = (b5 << 3) | (b0 & 0x07)
        buf[i + 1] = (b1 & 0xE0) | r5
        i += 2

def _tint_buf(buf, nbytes, tr, tg, tb, a):
    """Blend each pixel toward (tr,tg,tb). a is fixed-point 0..256."""
    inv = 256 - a
    i = 0
    while i < nbytes:
        b0 = buf[i]
        b1 = buf[i + 1]
        r5 = (b0 >> 3) & 0x1F
        g6 = ((b0 & 0x07) << 3) | ((b1 >> 5) & 0x07)
        b5 = b1 & 0x1F
        # Expand 5/6-bit -> 8-bit
        r8 = (r5 << 3) | (r5 >> 2)
        g8 = (g6 << 2) | (g6 >> 4)
        b8 = (b5 << 3) | (b5 >> 2)
        # Linear blend
        nr = (r8 * inv + tr * a) >> 8
        ng = (g8 * inv + tg * a) >> 8
        nb = (b8 * inv + tb * a) >> 8
        if nr > 255: nr = 255
        if ng > 255: ng = 255
        if nb > 255: nb = 255
        # Re-pack as RGB565 big-endian
        rr = (nr >> 3) & 0x1F
        gg = (ng >> 2) & 0x3F
        bb = (nb >> 3) & 0x1F
        buf[i]     = (rr << 3) | ((gg >> 3) & 0x07)
        buf[i + 1] = ((gg & 0x07) << 5) | bb
        i += 2

def _selective_recolor_buf(buf, nbytes, pink, leaf, bg_thresh):
    """'pink_body' style: true-black pixels stay pure black, green-
    dominant pixels (the leaf) recolor toward `leaf`, everything else
    (the body) recolors toward `pink`. Relative brightness/shading is
    preserved via a luminance-scaled factor instead of a flat blend,
    so highlights/shadows still read as shape, not a flat color fill.

    Verified against a Python/PIL reference implementation before
    being written here -- see the conversation's earlier preview
    GIFs for the confirmed look."""
    pr, pg, pb = pink
    lr, lg, lb = leaf
    i = 0
    while i < nbytes:
        b0 = buf[i]
        b1 = buf[i + 1]
        r5 = (b0 >> 3) & 0x1F
        g6 = ((b0 & 0x07) << 3) | ((b1 >> 5) & 0x07)
        b5 = b1 & 0x1F
        r8 = (r5 << 3) | (r5 >> 2)
        g8 = (g6 << 2) | (g6 >> 4)
        b8 = (b5 << 3) | (b5 >> 2)
        lum = (r8 + g8 + b8) // 3

        if lum < bg_thresh:
            nr = 0; ng = 0; nb = 0
        elif g8 >= r8 and g8 >= b8:
            factor = (lum * 256) // 130
            if factor > 332: factor = 332
            nr = (lr * factor) >> 8
            ng = (lg * factor) >> 8
            nb = (lb * factor) >> 8
        else:
            factor = (lum * 256) // 200
            if factor > 332: factor = 332
            nr = (pr * factor) >> 8
            ng = (pg * factor) >> 8
            nb = (pb * factor) >> 8

        if nr > 255: nr = 255
        if ng > 255: ng = 255
        if nb > 255: nb = 255

        rr = (nr >> 3) & 0x1F
        gg = (ng >> 2) & 0x3F
        bb = (nb >> 3) & 0x1F
        buf[i]     = (rr << 3) | ((gg >> 3) & 0x07)
        buf[i + 1] = ((gg & 0x07) << 5) | bb
        i += 2

# Precompute config so the per-frame check is one bool test
_TINT_R = 0
_TINT_G = 0
_TINT_B = 0
_TINT_A = 0
if COLOR_TINT is not None:
    _TINT_R, _TINT_G, _TINT_B = COLOR_TINT
    _TINT_A = max(0, min(256, int(COLOR_TINT_AMOUNT * 256)))

_RECOLOR_ACTIVE = (RECOLOR_STYLE == 'pink_body'
                   or (RECOLOR_STYLE == 'tint'
                       and (COLOR_INVERT
                            or COLOR_SWAP_RB
                            or (COLOR_TINT is not None and _TINT_A > 0))))

def apply_color_transform(buf, nbytes):
    """Apply the recolor selected by RECOLOR_STYLE, in place on `buf`."""
    if RECOLOR_STYLE == 'pink_body':
        _selective_recolor_buf(buf, nbytes, PINK_COLOR, LEAF_COLOR, BG_LUM_THRESH)
    elif RECOLOR_STYLE == 'tint':
        if COLOR_INVERT:
            _invert_buf(buf, nbytes)
        if COLOR_SWAP_RB:
            _swap_rb_buf(buf, nbytes)
        if COLOR_TINT is not None and _TINT_A > 0:
            _tint_buf(buf, nbytes, _TINT_R, _TINT_G, _TINT_B, _TINT_A)


# --- GYRO (MPU6050) ---
def read_accel():
    d = i2c.readfrom_mem(MPU_ADDR, 0x3B, 6)
    def s16(hi, lo):
        v = (hi << 8) | lo
        return v - 65536 if v > 32767 else v
    return s16(d[0], d[1]), s16(d[2], d[3]), s16(d[4], d[5])

def check_shake():
    """Returns True the frame a shake is detected (frame-to-frame jump
    in acceleration magnitude past SHAKE_THRESHOLD). Always returns
    False immediately if the gyro failed to initialize -- see _MPU_OK
    above -- so a disconnected/broken sensor never spams errors into
    the main loop, it just quietly does nothing."""
    global _last_accel_mag
    if not _MPU_OK:
        return False
    try:
        ax, ay, az = read_accel()
    except OSError:
        return False   # sensor hiccupped this frame; skip, don't crash
    mag = (ax * ax + ay * ay + az * az) ** 0.5
    shook = abs(mag - _last_accel_mag) > SHAKE_THRESHOLD
    _last_accel_mag = mag
    return shook


# --- LED (plain LED, brightness/blink patterns instead of color) ---
# (pattern, brightness_pct) per state. Edit these to taste.
LED_PATTERNS = {
    'idle':   ('solid',      50),
    'hangry': ('blink_slow', 100),
    'chonk':  ('solid',      100),
}
TRANSIENT_LED_PATTERNS = {
    'eating':      ('flash',      100),
    'sad':         ('blink_fast', 100),
    'getup':       ('blink_fast', 60),
    'grow':        ('pulse',      100),
    'hangry_once': ('blink_fast', 100),
}
_led_frame = 0

def tick_led():
    """Call once per main-loop iteration. Reads current base_state /
    transient_state directly, so nothing else needs to call this."""
    global _led_frame
    _led_frame += 1
    pattern, pct = (TRANSIENT_LED_PATTERNS.get(transient_state)
                     or LED_PATTERNS.get(base_state, ('off', 0)))
    max_duty = int(65535 * pct / 100)

    if pattern == 'solid':
        duty = max_duty
    elif pattern == 'off':
        duty = 0
    elif pattern == 'blink_slow':
        duty = max_duty if (_led_frame // 4) % 2 == 0 else 0
    elif pattern == 'blink_fast':
        duty = max_duty if (_led_frame // 1) % 2 == 0 else 0
    elif pattern == 'flash':
        duty = max_duty if (_led_frame % 8) < 2 else 0
    elif pattern == 'pulse':
        phase = _led_frame % 8
        level = phase if phase <= 4 else 8 - phase
        duty = int(max_duty * level / 4)
    else:
        duty = 0

    led.duty_u16(duty)


# --- SERVO (eased, non-blocking) ---
STATE_ANGLES = {'idle': 90, 'hangry': 60, 'chonk': 120}
TRANSIENT_ANGLES = {'eating': 100, 'sad': 45, 'getup': 90,
                     'grow': 90, 'hangry_once': 60}

def _servo_write(angle):
    ns = 500000 + int((angle / 180) * 2000000)
    servo.duty_ns(ns)

def set_servo_target():
    """Call whenever base_state/transient_state changes."""
    global _servo_target
    _servo_target = (TRANSIENT_ANGLES.get(transient_state)
                      or STATE_ANGLES.get(base_state, 90))

def tick_servo():
    """Call once per main-loop iteration; steps toward the target
    a few degrees at a time so it never blocks the animation loop."""
    global _servo_angle
    if _servo_angle != _servo_target:
        diff = _servo_target - _servo_angle
        if abs(diff) <= SERVO_STEP:
            _servo_angle = _servo_target
        else:
            _servo_angle += SERVO_STEP if diff > 0 else -SERVO_STEP
        _servo_write(_servo_angle)


# --- PUSH BUTTON: short press = interact, long press = sleep toggle ---
def check_button():
    """Returns 'short', 'long', or None. Call once per loop iteration."""
    global _press_start, asleep
    pressed = button.value() == 0
    if pressed and _press_start is None:
        _press_start = time.ticks_ms()
        return None
    if not pressed and _press_start is not None:
        held = time.ticks_diff(time.ticks_ms(), _press_start)
        _press_start = None
        if held >= LONG_PRESS_MS:
            asleep = not asleep
            return 'long'
        return 'short'
    return None


# --- SETUP ---
print("A: Booting...")
init_display()
print("B: Display initialized")

# --- PRECOMPUTE BAR POSITION ---
bar_row_start = (HEIGHT - BAR_HEIGHT) + BAR_Y_OFFSET
bar_row_start = max(0, min(HEIGHT - 1, bar_row_start))

rows_visible  = min(BAR_HEIGHT, HEIGHT - bar_row_start)
bar_byte_start = bar_row_start * ROW_BYTES

print("Bar position:",
      "row", bar_row_start,
      "to", bar_row_start + rows_visible - 1,
      "(" + str(rows_visible) + " rows visible)")
print("C: Mood bar ready (drawn live, no bar.bin needed)")

# --- INPUT POLLING ---
poll_obj = uselect.poll()
poll_obj.register(sys.stdin, uselect.POLLIN)

# --- BUFFERS ---
framebuf = bytearray(FRAME_SIZE)   # final composited frame sent to display
anim_buf = bytearray(FRAME_SIZE)   # raw animation frame from file

mv_frame = memoryview(framebuf)
mv_anim  = memoryview(anim_buf)

# Tiny 256-byte zero row, reused to blank only the uncovered strip
# each frame (see clear_uncovered_band()) -- replaces the old 32KB
# BLACK_FRAME constant, which was the actual cause of the ESP32
# MemoryError (three permanent 32KB buffers -- framebuf, anim_buf,
# and BLACK_FRAME -- added up to 96KB resident on a chip that
# typically only has ~100-115KB free).
_ZERO_ROW = bytes(ROW_BYTES)

# --- ANIMATION FILES ---
# Files live in the "art/" folder on the board's filesystem, matching
# the README upload instructions. Rename here if yours differ.
ART_DIR = "art/"
BASE_FILES = {
    'idle':   ART_DIR + "character_anim.bin",
    'hangry': ART_DIR + "hangry_anim.bin",
    'chonk':  ART_DIR + "chonk_anim.bin",
}
TRANSIENT_FILES = {
    'eating':      ART_DIR + "eating_anim.bin",
    'sad':         ART_DIR + "crying_anim.bin",
    'getup':       ART_DIR + "get-up_anim.bin",
    'grow':        ART_DIR + "grow_anim.bin",
    'hangry_once': ART_DIR + "hangry_anim.bin",
}

# --- ANIMATION ENGINE ---
anim_file         = None
anim_total_frames = 0
anim_current_frame = 0

def close_anim():
    global anim_file
    if anim_file:
        try:
            anim_file.close()
        except Exception:
            pass
        anim_file = None

def load_anim(filename):
    """Open `filename` for streaming frames. Falls back to no-draw on miss."""
    global anim_file, anim_total_frames, anim_current_frame
    close_anim()
    anim_current_frame = 0
    try:
        anim_file = open(filename, "rb")
        anim_total_frames = uos.stat(filename)[6] // FRAME_SIZE
        print("Loaded:", filename, "frames:", anim_total_frames)
    except OSError:
        anim_file = None
        anim_total_frames = 0
        print("Animation missing:", filename)

# --- STATE MACHINE ---
# base_state: looping animation (idle / hangry / chonk)
# transient_state: one-shot (eating / sad / getup / grow / hangry_once)
# pending_base: base to switch to once the transient chain finishes
base_state      = 'idle'
transient_state = None
pending_base    = None

def start_transient(name, after_base=None):
    global transient_state, pending_base
    if name not in TRANSIENT_FILES:
        return
    transient_state = name
    if after_base is not None:
        pending_base = after_base
    load_anim(TRANSIENT_FILES[name])
    set_servo_target()

def end_transient():
    """Called when a one-shot finishes. Resume base (or pending base)."""
    global transient_state, pending_base, base_state
    transient_state = None
    if pending_base is not None and pending_base != base_state:
        base_state = pending_base
    pending_base = None
    load_anim(BASE_FILES[base_state])
    set_servo_target()

def set_base(new_base):
    """Switch base state. If a transient is playing, queue it for after."""
    global base_state, pending_base
    if new_base not in BASE_FILES:
        return
    if transient_state is not None:
        pending_base = new_base
    elif new_base != base_state:
        base_state = new_base
        load_anim(BASE_FILES[base_state])
        set_servo_target()

def _chain_sad_to_getup():
    """After cryingAnim ends, play get-up before returning to base."""
    global transient_state
    if 'getup' not in TRANSIENT_FILES:
        end_transient()
        return
    transient_state = 'getup'
    load_anim(TRANSIENT_FILES['getup'])
    set_servo_target()

def _finish_transient_or_chain():
    """End one-shot, or go sad -> get-up when applicable."""
    if transient_state == 'sad' and 'getup' in TRANSIENT_FILES:
        _chain_sad_to_getup()
    else:
        end_transient()

def handle_command(ch):
    if   ch == 'e': start_transient('eating')
    elif ch == 's': start_transient('sad')
    elif ch in ('a', 'A'): start_transient('hangry_once')
    elif ch == 'g': start_transient('grow', after_base='chonk')
    elif ch == 'i': set_base('idle')
    elif ch == 'h': set_base('hangry')
    elif ch == 'c': set_base('chonk')

# Boot in idle by default; watcher will correct us on its first tick.
load_anim(BASE_FILES[base_state])

# --- MAIN LOOP ---
try:
    next_t = time.ticks_ms()

    while True:
        # Drain any pending commands from the watcher
        while poll_obj.poll(0):
            ch = sys.stdin.read(1)
            if ch:
                handle_command(ch)

        # Button: short press = interact/"pet", long press = sleep toggle.
        # Remap 'eating' below to whichever transient you want a short
        # press to trigger.
        press = check_button()
        if press == 'short' and not asleep:
            start_transient('eating')

        # Sleep mode: freeze LED/servo/animation, just keep polling the
        # button so a long press can wake it back up.
        if asleep:
            led.duty_u16(0)
            time.sleep_ms(FRAME_MS)
            continue

        # Shake: remap 'hangry_once' below to whichever transient you
        # want a shake to trigger.
        if check_shake():
            start_transient('hangry_once')

        tick_led()
        tick_servo()

        if anim_file is None or anim_total_frames == 0:
            # Unplayable animation. If it was a transient, skip it and
            # fall through to the pending/current base so we don't freeze.
            if transient_state is not None:
                print("Transient unplayable, skipping:", transient_state)
                end_transient()
                continue
            # Base is missing too - nothing we can draw, just idle
            time.sleep_ms(FRAME_MS)
            continue

        # 1) Load raw animation frame into temp buffer
        anim_file.seek(anim_current_frame * FRAME_SIZE)
        n = anim_file.readinto(mv_anim)

        if n != FRAME_SIZE:
            # Truncated/wrong file; never spin without sleep (keeps USB responsive)
            anim_current_frame = 0
            time.sleep_ms(10)
            continue

        # 1b) Runtime color transform (in place on anim_buf)
        if _RECOLOR_ACTIVE:
            apply_color_transform(anim_buf, FRAME_SIZE)

        # 2) Clear final framebuffer
        clear_uncovered_band()

        # 3) Composite shifted background
        composite_bg_with_offset()

        # 4) Composite bar on top
        composite_bar()

        # 5) Draw full frame in one SPI transaction
        set_window()
        spi.write(mv_frame)
        cs.value(1)

        # 6) Advance frame
        anim_current_frame += 1
        if anim_current_frame >= anim_total_frames:
            if transient_state is not None:
                # One-shot finished: sad -> get-up chain, or resume base
                _finish_transient_or_chain()
            else:
                # Base animation loops forever
                anim_current_frame = 0

        # 7) Stable frame pacing
        next_t = time.ticks_add(next_t, FRAME_MS)
        sleep_ms = time.ticks_diff(next_t, time.ticks_ms())

        if sleep_ms > 0:
            time.sleep_ms(sleep_ms)
        else:
            next_t = time.ticks_ms()

except KeyboardInterrupt:
    print("\nStopped by user")
except Exception as e:
    print("\nError:", e)

finally:
    close_anim()
    cs.value(1)
    print("Cleaned up and closed.")
