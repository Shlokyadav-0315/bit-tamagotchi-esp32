# Bit the Tamagotchi 🐷 — ESP32 Edition

A USB-drive-watching (and now much more) Tamagotchi that lives on an ESP32 and reacts to what you're doing on your computer.

**Based on [motino101/tamagotchi_hard_drive](https://github.com/motino101/tamagotchi_hard_drive)** — the original idea, art style, and USB-drive-watching concept came from that project, built for a Raspberry Pi Pico + Mac. This version is a substantial rework: ported to ESP32 + Windows, with a fully custom color scheme, a live mood bar, gyro shake detection, servo movement, physical button input, an LED heartbeat, on-screen text popups, and a custom-drawn sleep animation.

## What it does

Bit is a small pixel-art pet living on a 128×128 display, driven by an ESP32. A companion script on your PC watches your **Desktop** (originally a labeled USB drive) and reacts to real signals from your computer:

- **Add/delete a file** → eats happily / gets sad
- **Plug in your charger** → feeds it power
- **Battery gets low** → it gets hangry; critically low triggers a rage outburst
- **You open Chrome, Spotify, VS Code, Discord** → it perks up
- **You've been away 15+ minutes** → it gets lonely and sad; comes back happy when you return
- **Shake the physical board** (MPU-6050 gyro) → startled reaction
- **Press the button** → short press feeds it, long press puts it to sleep (with its own custom sleep animation)

All of this shows up on-screen as: the animated pet itself, a live color-coded **mood bar**, and temporary **pixel-art text popups** ("YUM!", "HANGRY!", etc.) that appear and fade on their own. A servo gives it physical movement, and an LED pulses like a heartbeat with a pattern that changes with its mood.

## Hardware

| Component | Notes |
|---|---|
| ESP32 DevKit (30-pin) | Any board with a CP2102/CH340 USB-serial chip works |
| 1.44" ST7735 TFT display (128×128) | **Must be the true 128×128 variant**, not the common 128×160 one |
| MPU-6050 accelerometer/gyro | For shake detection |
| SG90 micro servo | Needs its own 4×AA (6V) or regulated 5V power — do NOT power it from the ESP32's own rail |
| 6×6mm tactile push button | Any basic momentary switch |
| 5mm LED + 220-330Ω resistor | Heartbeat indicator |
| Breadboard(s) + jumper wires | No soldering required |

### Wiring

| Signal | ESP32 GPIO |
|---|---|
| Display SCK | 18 |
| Display MOSI | 23 |
| Display CS | 5 |
| Display RST | 26 |
| Display DC | 27 |
| Gyro SDA | 21 |
| Gyro SCL | 22 |
| LED | 4 |
| Servo signal | 13 |
| Button | 14 |

Display backlight (BLK) and VCC → 3.3V. Servo power → separate external supply, ground shared with the ESP32.

## Software setup

1. Flash **MicroPython** onto the ESP32 (via Thonny — Tools → "Install or update MicroPython").
2. Upload `main.py` and the entire `art/` folder to the board's root.
3. On your PC: `pip install pyserial psutil`
4. Run `watcher.py` — it auto-detects the ESP32's serial port and starts watching your Desktop.

## What's different from the original

- Ported from Raspberry Pi Pico (RP2040) + SSD1351 OLED to **ESP32 + ST7735 TFT** — different display driver, different pin architecture, no `@micropython.viper` support on ESP32 so those code paths were rewritten in plain Python
- Ported the watcher script from macOS to **Windows** (drive auto-detection via the Windows registry instead of a fixed `/Volumes/` path, ESP32 USB-chip detection instead of Raspberry Pi's vendor ID)
- Custom **pink/black/green** color palette, baked permanently into the art files for zero runtime performance cost
- Watches your real **Desktop** instead of requiring a dedicated, specially-labeled USB flash drive
- **Battery awareness** and **inactivity/loneliness detection** — reactions driven by real laptop signals, not just files
- **App-launch detection** (Chrome, Spotify, VS Code, Discord, etc.)
- Procedurally-drawn, live **mood bar** (no static image needed)
- **MPU-6050 shake detection**, **SG90 servo movement**, **physical button input**, and an **LED heartbeat** — all synced to the same animation state machine
- **Pixel-art text popups** rendered with a hand-built bitmap font, entirely in code
- A custom hand-drawn **sleep animation**

## Credits

- Original concept, Pico/Mac implementation, and base pixel art: [motino101](https://github.com/motino101/tamagotchi_hard_drive)
- ESP32/Windows port, new features, and additional art: [your name here]
