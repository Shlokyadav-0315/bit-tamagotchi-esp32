"""
TAMAGOTCHI WATCHER - WINDOWS + ESP32 PORT (v2: real laptop signals)

Drives the board via single-byte serial commands (see main.py for .bin names):
  e = eating_anim (file added / laptop plugged in / you came back)
  s = crying_anim -> get-up_anim (file removed / you've been away a while)
  a = hangry_anim outburst (file-delete streak 5+ / battery critical)
  g = grow_anim then chonk (first cross into chonk, 10+ files on Desktop)
  i / h / c = base: character / hangry / chonk

Base state priority (highest wins):
  1. Battery LOW or CRITICAL (unplugged)  -> h (hangry)
  2. Desktop file count                   -> h (0 files) / i (1-9) / c (10+)

--------------------------------------------------------------------
WHAT'S NEW IN v2 (on top of the Windows+ESP32 port):

  1. WATCHES YOUR REAL DESKTOP instead of requiring a labeled USB
     flash drive. No more renaming a drive to "hey im bit" -- it
     just watches ~/Desktop by default. Add/delete a file there,
     same eating/sad/streak logic as before, just pointed at
     something you already use. Override WATCH_DIR below to point
     at a different folder if you'd rather.

  2. BATTERY AWARENESS (needs `pip install psutil`):
       - Plugging in your charger fires the eating animation --
         "feeding" it power is a genuinely natural metaphor.
       - Battery below LOW_BATTERY_PERCENT (default 20%, unplugged)
         forces the hangry base state -- low battery IS hangry.
       - Battery below CRITICAL_BATTERY_PERCENT (default 5%,
         unplugged) additionally fires the hangry rage one-shot,
         once, on the way down.
       - Recovering (plugging in, or charging back above the
         threshold) hands control back to the normal file-count
         base state.
     On a desktop PC with no battery, this feature silently does
     nothing (psutil reports no battery present) -- no crash, no
     spam, it just never fires.

  3. LONELINESS (no extra installs -- uses ctypes + Windows'
     built-in GetLastInputInfo, the same API Windows itself uses
     to decide when to lock your screen or dim the display):
       - No mouse/keyboard input for INACTIVITY_MINUTES (default
         15) fires the sad animation once -- this is the classic
         real-Tamagotchi "you ignored me" mechanic, and honestly
         the one most likely to make it feel alive.
       - The moment you touch the mouse/keyboard again after that,
         it fires the eating/happy animation once as a little
         "welcome back" -- then resumes whatever base state applies.

  WHY YOUTUBE-WATCHING ISN'T HERE: it doesn't map cleanly onto any
  existing emotion without stretching the metaphor (is watching a
  video "eating"? "growing"? neither really fits), so it's left out
  for now rather than forced in. The active-window-title technique
  it would need (reading GetForegroundWindow's title via ctypes) is
  the same category of trick as the loneliness detector above, so
  it's a small addition later once you decide what it should mean.
--------------------------------------------------------------------
"""
import os
import ctypes
import time
import random
import serial
from serial.tools import list_ports
from datetime import datetime

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# --- CONFIGURATION ---
# "auto" = your real Desktop folder (~/Desktop). Override with a
# literal path (e.g. "D:\\SomeFolder") to watch somewhere else.
WATCH_DIR = "auto"

# "auto" = scan USB serial ports for a recognized ESP32 USB-UART
# chip on every (re)connect attempt.
# Override with a literal COM port (e.g. "COM5") to pin one instead.
BOARD_PORT = "auto"
BAUD_RATE  = 115200
CHONK_THRESHOLD = 10  # >=10 files = chonk

# --- battery thresholds (only used if psutil detects a battery) ---
LOW_BATTERY_PERCENT      = 50
CRITICAL_BATTERY_PERCENT = 20

# --- loneliness threshold ---
INACTIVITY_MINUTES = 5
INACTIVITY_SECONDS = INACTIVITY_MINUTES * 60

# USB Vendor IDs for the common ESP32 dev-board USB-to-serial chips.
_ESP32_USB_VIDS = {
    0x10C4,  # Silicon Labs CP2102 / CP2104
    0x1A86,  # WCH CH340 / CH341
    0x0403,  # FTDI
    0x303A,  # Espressif (native USB CDC on S2/S3/C3)
}


def resolve_watch_dir():
    """Return the folder to watch -- your real Desktop by default."""
    if WATCH_DIR not in ("", "auto", None):
        return WATCH_DIR

    # Ask Windows itself where the Desktop really is, instead of
    # assuming the classic C:\Users\<you>\Desktop path. If OneDrive
    # folder backup is turned on (very common default on Windows 11),
    # your visible Desktop is silently redirected to somewhere like
    # C:\Users\<you>\OneDrive\Desktop -- and watching the wrong one
    # means file changes never get seen, with zero error messages.
    # This reads the same registry value File Explorer itself uses,
    # so it always matches what you actually see on your desktop.
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        raw_path, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        return os.path.expandvars(raw_path)   # expands %USERPROFILE% etc.
    except Exception:
        # Fallback for non-Windows or if the registry read fails
        return os.path.join(os.path.expanduser("~"), "Desktop")


def find_pico_port():
    """Return the device path (COM port) of a connected ESP32 board,
    or None."""
    candidates = []
    for p in list_ports.comports():
        if p.vid in _ESP32_USB_VIDS:
            candidates.append(p)
            continue
        mfr = (p.manufacturer or "").lower()
        desc = (p.description or "").lower()
        product = (p.product or "").lower()
        if (
            "cp210" in desc or "cp210" in product
            or "ch340" in desc or "ch340" in product
            or "ch341" in desc or "ch341" in product
            or "silicon labs" in mfr
            or "wch.cn" in mfr
            or "usb-serial" in desc
            or "usb to uart" in desc
            or "esp32" in desc or "esp32" in product
        ):
            candidates.append(p)
    if not candidates:
        return None
    return candidates[0].device


# --- IDLE-TIME DETECTION (Windows built-in, no extra installs) ---
class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]

def get_idle_seconds():
    """Seconds since the last mouse/keyboard input, system-wide.
    Same underlying Windows API used to decide when to dim your
    screen or trigger a lock-screen timeout."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return millis / 1000.0


# --- BATTERY READING ---
def get_battery():
    """Returns (percent, plugged) or (None, None) if no battery
    (e.g. a desktop PC) or psutil isn't installed."""
    if not _HAS_PSUTIL:
        return None, None
    batt = psutil.sensors_battery()
    if batt is None:
        return None, None
    return batt.percent, batt.power_plugged


# --- ANSI COLORS (so the terminal looks alive) ---
class C:
    RESET   = "\033[0m"
    DIM     = "\033[2m"
    BOLD    = "\033[1m"
    PINK    = "\033[95m"
    CYAN    = "\033[96m"
    YELLOW  = "\033[93m"
    GREEN   = "\033[92m"
    RED     = "\033[91m"
    BLUE    = "\033[94m"
    GREY    = "\033[90m"

# --- PERSONALITY: faces + flavor phrases ---
FACES = {
    'idle':   "(•_•)",
    'hangry': "(>_<)",
    'chonk':  "(✿◠‿◠)",
    'eat':    "(◕‿◕)~🍎",
    'sad':    "(╥﹏╥)",
    'grow':   "✧･ﾟ(☉益☉)ﾟ･✧",
    'rage':   "(눈_눈)",
}

EAT_PHRASES = [
    "OM NOM NOM!",
    "*chomp chomp*",
    "DELICIOUS!!",
    "yum yum yum",
    "more pls more",
    "*munching intensifies*",
    "tasty bytes!",
]

SAD_PHRASES = [
    "*sniffle*",
    "where did it gooo",
    "i miss my snack",
    "noooooo",
    "*single pixel tear*",
    "why u do dis",
]

HANGRY_RAGE_PHRASES = [
    "TOO MANY CHANGES!!",
    "*hangry screech*",
    "STOP. DELETING. THINGS.",
    "i have HAD it—",
    "this is the LAST straw",
]

GROW_PHRASES = [
    "GROWTH SPURT!!",
    "BEHOLD, THE CHONK",
    "*absolute unit incoming*",
    "i am become big",
    "EVOLVING...",
    "thicc protocol engaged",
]

CHARGE_PHRASES = [
    "MMM, ELECTRONS!!",
    "*slurps voltage*",
    "power snack time",
    "charging = feeding, fight me",
    "yesss juice me up",
]

LOW_BATTERY_PHRASES = [
    "i'm running on fumes...",
    "battery hangry...",
    "feed me electrons pls",
    "getting weak here",
]

CRITICAL_BATTERY_PHRASES = [
    "I'M DYING—",
    "PLUG ME IN. NOW.",
    "*final battery scream*",
    "5%?!?! UNACCEPTABLE",
]

LONELY_PHRASES = [
    "*chirp* ...hello?",
    "did you forget about me",
    "it's quiet in here...",
    "i'll just wait then...",
    "*stares at nothing*",
]

REUNION_PHRASES = [
    "YOU'RE BACK!!",
    "MISSED YOU!!",
    "finally, a human",
    "*happy vibrating*",
]

# --- STREAKS ---
# Streak resets if no matching event for this many seconds, OR opposite event occurs.
STREAK_WINDOW = 300  # 5 minutes

# (threshold, fire-icon, phrase) — first match >= n wins
EAT_STREAK_TIERS = [
    (20, "🔥👑", "LEGENDARY {n}× STREAK!!"),
    (15, "🔥💥", "UNSTOPPABLE — {n} in a row!"),
    (10, "🔥🔥🔥", "{n}× COMBO!! pet is BLESSED"),
    (7,  "🔥🔥",   "ON FIRE — {n} snacks back to back!"),
    (5,  "🔥",     "{n} STREAK!! pet is thriving"),
    (3,  "✨",     "{n} in a row!"),
]

SAD_STREAK_TIERS = [
    (7, "😭⚠️", "pet emotional damage: {n}"),
    (5, "😭💔", "{n} losses... i give up"),
    (3, "😭",   "{n} in a row... stop bullying meee"),
]

BASE_LABELS = {'i': 'idle', 'h': 'hangry', 'c': 'chonk'}

# --- helpers ---
def stamp():
    return f"{C.GREY}[{datetime.now().strftime('%H:%M:%S')}]{C.RESET}"

def say(icon, color, msg, face=""):
    tail = f"  {color}{face}{C.RESET}" if face else ""
    print(f"{stamp()} {icon} {color}{msg}{C.RESET}{tail}")

def pico_say(line):
    pretty = line
    if line.startswith("Loaded:"):
        try:
            _, rest = line.split("Loaded:", 1)
            name_part, frames_part = rest.split("frames:")
            name = name_part.strip().replace(".bin", "")
            n = frames_part.strip()
            pretty = f"loaded animation '{name}' ({n} frames)"
        except Exception:
            pass
    print(f"{stamp()} {C.DIM}🤖 board whispers: {pretty}{C.RESET}")

def count_files(directory):
    total = 0
    try:
        for root, dirs, files in os.walk(directory):
            files = [f for f in files if not f.startswith('.')]
            total += len(files)
    except Exception:
        return 0
    return total

def get_file_base_state(count):
    """Return the base-state command character for a given file count."""
    if count <= 0:
        return 'h'                 # hangry
    if count >= CHONK_THRESHOLD:
        return 'c'                 # chonk
    return 'i'                     # idle

def streak_message(n, tiers):
    for threshold, icon, phrase in tiers:
        if n >= threshold:
            return icon, phrase.format(n=n)
    return None

def show_streak(n, tiers, color):
    hit = streak_message(n, tiers)
    if not hit:
        return
    prev = streak_message(n - 1, tiers)
    if prev and prev[0] == hit[0]:
        return
    icon, phrase = hit
    print(f"{stamp()}    {color}└─ {icon} {phrase}{C.RESET}")

def banner():
    print(f"{C.PINK}╔══════════════════════════════════════════╗")
    print(f"║  {C.BOLD}🐣  TAMAGOTCHI  WATCHER  🐣{C.RESET}{C.PINK}             ║")
    print(f"╚══════════════════════════════════════════╝{C.RESET}")

def open_pico(announce_waiting=True):
    waiting_announced = False
    error_announced = False
    while True:
        port = BOARD_PORT if BOARD_PORT not in ("", "auto", None) else find_pico_port()
        if not port:
            if announce_waiting and not waiting_announced:
                say("🔍", C.CYAN, "waiting for tamagotchi… plug the ESP32 in via USB")
                waiting_announced = True
            time.sleep(1.0)
            continue
        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
            say("🔌", C.GREEN, f"connected to {port}")
            return ser, port
        except serial.SerialException as e:
            if not error_announced:
                say("💔", C.RED, f"found board at {port} but couldn't open it ({e})")
                say("  ", C.DIM, "is Thonny / another script / the Arduino Serial Monitor holding it?")
                error_announced = True
            time.sleep(2.0)


def main():
    banner()

    watch_dir = resolve_watch_dir()
    say("👀", C.CYAN, f"watching: {watch_dir}")

    if not _HAS_PSUTIL:
        say("⚠️", C.YELLOW, "psutil not installed -- battery awareness disabled")
        say("  ", C.DIM, "run: pip install psutil   (then restart this script)")

    ser, active_port = open_pico()

    last_count = count_files(watch_dir)
    file_base  = get_file_base_state(last_count)
    current_base = file_base

    ser.write(current_base.encode())
    label = BASE_LABELS[current_base]
    say("📁", C.YELLOW, f"starting count: {last_count} files → {label}",
        face=FACES.get(label, ""))
    say("👂", C.DIM, "listening for board whispers...")

    # --- file streak state ---
    eat_streak = 0
    sad_streak = 0
    last_event_ts = 0.0

    # --- battery state ---
    battery_state = 'normal'   # 'normal' | 'low' | 'critical'
    last_plugged  = None       # None until we've read it once

    # --- loneliness state ---
    lonely = False

    while True:
        try:
            # --- 0. MAKE SURE THE WATCH FOLDER IS STILL THERE ---
            if not os.path.isdir(watch_dir):
                say("📁❌", C.YELLOW, f"can't find {watch_dir} — waiting for it to reappear…")
                time.sleep(2.0)
                continue

            # --- 1. READ LOGS FROM BOARD ---
            try:
                waiting = ser.in_waiting
            except (serial.SerialException, OSError) as e:
                say("🔌💤", C.YELLOW, f"lost {active_port} ({e}) — reconnecting…")
                try:
                    ser.close()
                except Exception:
                    pass
                ser, active_port = open_pico()
                ser.write(current_base.encode())
                continue
            if waiting > 0:
                try:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        pico_say(line)
                except (serial.SerialException, OSError) as e:
                    say("🔌💤", C.YELLOW, f"lost {active_port} ({e}) — reconnecting…")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser, active_port = open_pico()
                    ser.write(current_base.encode())
                    continue
                except Exception as e:
                    say("⚠️", C.RED, f"serial read error: {e}")

            # --- 2. WATCH DESKTOP FILES ---
            current_count = count_files(watch_dir)

            if current_count != last_count:
                new_file_base = get_file_base_state(current_count)
                now = time.time()

                if now - last_event_ts > STREAK_WINDOW:
                    eat_streak = 0
                    sad_streak = 0

                if current_count > last_count:
                    sad_streak = 0
                    eat_streak += 1

                    if file_base != 'c' and new_file_base == 'c':
                        ser.write(b'g')
                        say("💪✨", C.PINK,
                            f"{random.choice(GROW_PHRASES)}  ({last_count} → {current_count})",
                            face=FACES['grow'])
                    else:
                        ser.write(b'e')
                        say("🍎", C.GREEN,
                            f"{random.choice(EAT_PHRASES)}  ({last_count} → {current_count})",
                            face=FACES['eat'])

                    show_streak(eat_streak, EAT_STREAK_TIERS, C.YELLOW)
                else:
                    eat_streak = 0
                    sad_streak += 1

                    if sad_streak >= 5:
                        ser.write(b'a')
                        say("🌶️", C.RED,
                            f"{random.choice(HANGRY_RAGE_PHRASES)}  "
                            f"({last_count} → {current_count})",
                            face=FACES['rage'])
                    else:
                        ser.write(b's')
                        say("😢", C.BLUE,
                            f"{random.choice(SAD_PHRASES)}  ({last_count} → {current_count})",
                            face=FACES['sad'])

                    show_streak(sad_streak, SAD_STREAK_TIERS, C.BLUE)

                last_event_ts = now
                last_count = current_count
                file_base  = new_file_base

            # --- 3. BATTERY AWARENESS ---
            if _HAS_PSUTIL:
                percent, plugged = get_battery()
                if percent is not None:
                    # Plugging in -> feed it
                    if last_plugged is False and plugged is True:
                        ser.write(b'e')
                        say("🔌🍎", C.GREEN,
                            f"{random.choice(CHARGE_PHRASES)}  ({percent:.0f}%)",
                            face=FACES['eat'])
                    last_plugged = plugged

                    if not plugged and percent <= CRITICAL_BATTERY_PERCENT:
                        if battery_state != 'critical':
                            ser.write(b'a')
                            say("🪫💥", C.RED,
                                f"{random.choice(CRITICAL_BATTERY_PHRASES)}  ({percent:.0f}%)",
                                face=FACES['rage'])
                        battery_state = 'critical'
                    elif not plugged and percent <= LOW_BATTERY_PERCENT:
                        if battery_state == 'normal':
                            say("🪫", C.YELLOW,
                                f"{random.choice(LOW_BATTERY_PHRASES)}  ({percent:.0f}%)",
                                face=FACES['hangry'])
                        battery_state = 'low'
                    else:
                        battery_state = 'normal'

            # --- 4. LONELINESS (idle mouse/keyboard) ---
            idle_secs = get_idle_seconds()
            if not lonely and idle_secs >= INACTIVITY_SECONDS:
                lonely = True
                ser.write(b's')
                say("💤😢", C.BLUE,
                    f"{random.choice(LONELY_PHRASES)}  (idle {INACTIVITY_MINUTES}+ min)",
                    face=FACES['sad'])
            elif lonely and idle_secs < 2.0:
                lonely = False
                ser.write(b'e')
                say("🎉", C.GREEN, f"{random.choice(REUNION_PHRASES)}", face=FACES['eat'])

            # --- 5. RECOMPUTE DESIRED BASE (battery overrides file count) ---
            desired_base = 'h' if battery_state in ('low', 'critical') else file_base
            if desired_base != current_base:
                ser.write(desired_base.encode())
                new_label = BASE_LABELS[desired_base]
                print(f"{stamp()}    {C.DIM}└─ mood: "
                      f"{BASE_LABELS[current_base]} → {new_label}  "
                      f"{FACES.get(new_label, '')}{C.RESET}")
                current_base = desired_base

            time.sleep(0.5)

        except KeyboardInterrupt:
            print()
            say("👋", C.PINK, "bye bye! pet is taking a nap...", face="(­ ˘ ω ˘ )zzZ")
            ser.close()
            break
        except Exception as e:
            say("⚠️", C.RED, f"error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
