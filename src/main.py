# main.py for Raspberry Pi Pico W
# Title: Pico Light Orchestra – Wi-Fi control + smooth light→pitch (transpose+vibrato) + melody queue + /tone

import machine
import time
import network
import json
import asyncio
import ubinascii
import math

# --- Hardware ---
photo_sensor_pin = machine.ADC(28)             # LDR on ADC28
buzzer_pin = machine.PWM(machine.Pin(16))      # Piezo on GP16 (PWM)

# --- API / Tasks ---
API_VERSION = "1.0.0"
api_note_task = None     # used only for /tone
# melody queue/task is cooperative via note_queue + melody_player()

# ---------- Queue fallback (uasyncio may not have asyncio.Queue) ----------
def _make_queue():
    try:
        _ = asyncio.Queue  # may raise AttributeError on some firmwares
        return asyncio.Queue()
    except AttributeError:
        pass

    class SimpleQueue:
        def __init__(self):
            self._items = []
            self._evt = asyncio.Event()
            self._evt.set()

        async def put(self, item):
            self._items.append(item)
            self._evt.set()

        async def get(self):
            while True:
                if self._items:
                    return self._items.pop(0)
                self._evt.clear()
                await self._evt.wait()

        def get_nowait(self):
            if not self._items:
                raise IndexError("queue empty")
            return self._items.pop(0)

    return SimpleQueue()

note_queue = _make_queue()
stop_now = asyncio.Event()   # aborts current note & gaps when set()

# --- Light calibration (YOUR MEASUREMENTS) ---
# darkest ~65467, brightest ~1896, and brighter => raw goes DOWN
LIGHT_INVERT   = True
LIGHT_MIN_RAW  = 1896     # brightest raw
LIGHT_MAX_RAW  = 65467    # darkest raw
LIGHT_GAMMA    = 1.0      # tweak to change response curve (e.g., 0.8 or 1.3)

# --- Smooth pitch control from light (no amplitude gating) ---
FRAME_MS        = 10      # update pitch every 10 ms (100 Hz control)
DARK_EDGE       = 33      # % thresholds (used only for /sensor_full info)
BRIGHT_EDGE     = 67

# Pitch transpose range in semitones (dark -> bright)
TRANSPOSE_MIN   = -7      # perfect fifth down
TRANSPOSE_MAX   = +7      # perfect fifth up

# Vibrato (pitch wobble) parameters
VIBRATO_HZ         = 6.0   # musical vibrato rate
VIBRATO_DEPTH_MIN  = 0.00  # 0% at darkest
VIBRATO_DEPTH_MAX  = 0.02  # 2% at brightest (~1/6 semitone) – subtle but clear

# Light smoothing so pitch doesn't jitter
_norm_prev = None
NORM_ALPHA = 0.20     # 0..1 (higher = more responsive, lower = smoother)

# ---------- Utilities ----------
def get_device_id() -> str:
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    mac = wlan.config("mac")
    return "pico-w-" + ubinascii.hexlify(mac).decode("utf-8").upper()

def connect_to_wifi(wifi_config: str = "wifi_config.json") -> str:
    with open(wifi_config, "r") as f:
        data = json.load(f)
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(data.get("ssid", ""), data.get("password", ""))

    max_wait = 20
    while max_wait > 0 and not (wlan.status() < 0 or wlan.status() >= 3):
        max_wait -= 1
        time.sleep(1)
    if wlan.status() != 3:
        raise RuntimeError("Network connection failed")
    return wlan.ifconfig()[0]

# ---------- Light normalization ----------
def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

def read_light_raw(samples: int = 4) -> int:
    total = 0
    for _ in range(samples):
        total += photo_sensor_pin.read_u16()
        time.sleep_ms(2)
    return total // samples

def light_raw_to_norm(raw: int) -> float:
    raw = _clamp(raw, LIGHT_MIN_RAW, LIGHT_MAX_RAW)
    span = LIGHT_MAX_RAW - LIGHT_MIN_RAW
    if span <= 0:
        return 0.0
    lin = (raw - LIGHT_MIN_RAW) / span  # 0..1 growing with raw
    if LIGHT_INVERT:
        lin = 1.0 - lin                  # make it grow with brightness
    g = LIGHT_GAMMA if LIGHT_GAMMA > 0 else 1.0
    return lin ** g

def _norm_to_percent(norm: float) -> int:
    p = int(round(norm * 100))
    return 0 if p < 0 else 100 if p > 100 else p

def _map(val, a0, a1, b0, b1):
    # linear map val∈[a0,a1] -> [b0,b1]
    if a1 == a0:
        return b0
    f = (val - a0) / (a1 - a0)
    if f < 0: f = 0
    if f > 1: f = 1
    return b0 + f * (b1 - b0)

def light_to_pitch_params():
    """
    Returns:
      transpose_semitones (float), vibrato_depth (0..~0.02), smoothed_norm (0..1)
    based on *smoothed* light level.
    """
    global _norm_prev
    raw  = read_light_raw()
    norm = light_raw_to_norm(raw)  # 0..1

    # exponential smoothing
    if _norm_prev is None:
        _norm_prev = norm
    else:
        _norm_prev = (NORM_ALPHA * norm) + ((1.0 - NORM_ALPHA) * _norm_prev)

    # map smoothed norm -> semitone transpose & vibrato depth
    transpose = _map(_norm_prev, 0.0, 1.0, TRANSPOSE_MIN, TRANSPOSE_MAX)
    vib_depth = _map(_norm_prev, 0.0, 1.0, VIBRATO_DEPTH_MIN, VIBRATO_DEPTH_MAX)
    return transpose, vib_depth, _norm_prev

# ---------- Buzzer control ----------
def stop_tone():
    buzzer_pin.duty_u16(0)

# ---------- Single-tone player for /tone ----------
async def play_tone_once(freq: int, ms: int, duty: float):
    """Play a single tone for ms milliseconds with given duty (0.0..1.0)."""
    try:
        if freq > 0 and ms > 0 and duty > 0:
            buzzer_pin.freq(int(freq))
            u16 = int(duty * 65535)
            if u16 < 0: u16 = 0
            if u16 > 65535: u16 = 65535
            buzzer_pin.duty_u16(u16)
            await asyncio.sleep(ms / 1000)
        stop_tone()
    except asyncio.CancelledError:
        stop_tone()
        raise

# ---------- Melody queue player (pitch-modulated by light) ----------
async def melody_player():
    while True:
        freq, ms, gap_ms = await note_queue.get()
        if stop_now.is_set():
            stop_tone()
            continue

        if freq > 0 and ms > 0:
            base_freq = int(freq)
            remaining = int(ms)
            t = 0.0
            while remaining > 0:
                if stop_now.is_set():
                    break
                transpose, vib_depth, _ = light_to_pitch_params()
                trans_mult = math.pow(2.0, transpose / 12.0)
                vib = math.sin(2.0 * math.pi * VIBRATO_HZ * t) * vib_depth

                f_now = int(max(1, base_freq * trans_mult * (1.0 + vib)))
                buzzer_pin.freq(f_now)
                buzzer_pin.duty_u16(32768)  # steady clean tone

                await asyncio.sleep(FRAME_MS / 1000.0)
                remaining -= FRAME_MS
                t += FRAME_MS / 1000.0

            buzzer_pin.duty_u16(0)
        else:
            # REST (or invalid)
            buzzer_pin.duty_u16(0)
            await asyncio.sleep(max(ms, 0) / 1000)

        if not stop_now.is_set() and gap_ms > 0:
            await asyncio.sleep(gap_ms / 1000)
        else:
            stop_tone()

# ---------- HTTP server ----------
async def handle_request(reader, writer):
    global api_note_task

    req_line = await reader.readline()
    # Skip headers
    while await reader.readline() != b"\r\n":
        pass
    try:
        method, url, _ = str(req_line, "utf-8").split()
    except Exception:
        writer.write(b"HTTP/1.0 400 Bad Request\r\n\r\n")
        await writer.drain(); writer.close(); await writer.wait_closed(); return

    # ---- Routes ----
    # Home
    if method == "GET" and url == "/":
        raw = read_light_raw()
        norm = light_raw_to_norm(raw)
        body = (
            "<html><body><h1>Pico Light Orchestra</h1>"
            f"<p>Light raw: {raw}</p>"
            f"<p>Light norm: {norm:.2f}</p>"
            "</body></html>"
        )
        response = body
        content_type = "text/html"
        status_line = "HTTP/1.0 200 OK\r\n"

    # Basic sensor
    elif method == "GET" and url == "/sensor":
        raw = read_light_raw()
        norm = light_raw_to_norm(raw)
        lux_est = norm * 200.0  # simple scaled number

        response = (
            '{\n'
            '  "raw": ' + str(raw) + ',\n'
            '  "norm": ' + ('%.2f' % norm) + ',\n'
            '  "lux_est": ' + ('%.1f' % lux_est) + '\n'
            '}'
        )
        content_type = "application/json"
        status_line = "HTTP/1.0 200 OK\r\n"

    # Health
    elif method == "GET" and url == "/health":
        response = (
            '{\n'
            '  "status":"ok",\n'
            '  "device_id":"' + get_device_id() + '",\n'
            '  "api":"' + API_VERSION + '"'
            '\n}'
        )
        content_type = "application/json"
        status_line = "HTTP/1.0 200 OK\r\n"


    # NEW: Plays a single tone immediately; cancels any tone/melody
    # Request: {"freq": 440, "ms": 300, "duty": 0.5}
    # Response: 202 Accepted {"playing": true, "until_ms_from_now": 300}
    elif method == "POST" and url == "/tone":
        raw_data = await reader.read(4096)
        try:
            data = json.loads(raw_data)
            freq = int(data.get("freq", 0))
            ms   = int(data.get("ms", 0))
            duty = float(data.get("duty", 0.5))
            # clamp duty
            if duty < 0.0: duty = 0.0
            if duty > 1.0: duty = 1.0

            # cancel any running single-tone task
            if api_note_task:
                api_note_task.cancel()
                try: await api_note_task
                except Exception: pass
                api_note_task = None

            # cancel any queued melody playback
            stop_now.set()
            cleared = 0
            try:
                while True:
                    _ = note_queue.get_nowait()
                    cleared += 1
            except Exception:
                pass

            # schedule new tone
            api_note_task = asyncio.create_task(play_tone_once(freq, ms, duty))

            response = json.dumps({
                "playing": True if (freq > 0 and ms > 0 and duty > 0) else False,
                "until_ms_from_now": ms
            })
            content_type = "application/json"
            status_line = "HTTP/1.0 202 Accepted\r\n"

        except Exception:
            writer.write(b'HTTP/1.0 400 Bad Request\r\n\r\n{"error":"Invalid JSON"}\r\n')
            await writer.drain(); writer.close(); await writer.wait_closed(); return

    # Melody queue (light-modulated pitch)
    # Request: {"notes":[{"freq":523,"ms":200}, ...], "gap_ms":20}
    elif method == "POST" and url == "/melody":
        raw_data = await reader.read(65536)
        try:
            data = json.loads(raw_data)
            notes = data.get("notes", [])
            gap_ms = int(data.get("gap_ms", 50))

            count = 0
            for n in notes:
                if isinstance(n, dict):
                    freq = int(n.get("freq", 0))
                    ms   = int(n.get("ms", 0))
                    if ms > 0:
                        await note_queue.put((freq, ms, gap_ms))
                        count += 1

            # allow playback if previously stopped
            if stop_now.is_set():
                stop_now.clear()

            response = '{\n  "queued": %d\n}' % count  # pretty for MicroPython
            content_type = "application/json"
            status_line = "HTTP/1.0 200 OK\r\n"
        except Exception:
            writer.write(b'HTTP/1.0 400 Bad Request\r\n\r\n{"error":"Invalid melody JSON"}\r\n')
            await writer.drain(); writer.close(); await writer.wait_closed(); return

    # Stop everything (tone + melody)
    elif method == "POST" and url == "/stop":
        if api_note_task:
            api_note_task.cancel()
            try: await api_note_task
            except Exception: pass
            api_note_task = None

        stop_now.set()
        cleared = 0
        try:
            while True:
                _ = note_queue.get_nowait()
                cleared += 1
        except Exception:
            pass

        stop_tone()
        response = json.dumps({"status": "ok", "message": "Stopped.", "cleared": cleared})
        content_type = "application/json"
        status_line = "HTTP/1.0 200 OK\r\n"

    else:
        writer.write(b"HTTP/1.0 404 Not Found\r\n\r\n")
        await writer.drain(); writer.close(); await writer.wait_closed(); return

    # Send response
    writer.write(f"{status_line}Content-type: {content_type}\r\n\r\n".encode("utf-8"))
    writer.write(response.encode("utf-8"))
    await writer.drain(); writer.close(); await writer.wait_closed()

# ---------- Main ----------
async def main():
    try:
        ip = connect_to_wifi()
        print("Connected on", ip)
        _ = await asyncio.start_server(handle_request, "0.0.0.0", 80)
        print("HTTP server listening on http://%s/" % ip)

        _ = asyncio.create_task(melody_player())
        while True:
            await asyncio.sleep(1)
    except Exception as e:
        print("Init failed:", e)
        stop_tone()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        stop_tone()
        print("Program stopped.")

