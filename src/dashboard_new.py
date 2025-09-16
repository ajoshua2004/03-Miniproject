# dashboard.py — single-Pico, lean version
import requests 
import time
import sys
from datetime import datetime, timedelta, UTC

# --- Config and Formatting Constants ---
PICO_IP = "172.20.10.8"    # 
POLL_INTERVAL = 1.0        # s
HTTP_TIMEOUT = 1.0         # s
LATENCY_WARN_MS = 150
STALE_AFTER_SEC = 5

CSI = "\x1b["
RESET = CSI + "0m"
BOLD = CSI + "1m"
RED = CSI + "31m"
YELLOW = CSI + "33m"
GREEN = CSI + "32m"

def color(text, c): 
    return f"{c}{text}{RESET}"

def clear(): 
    sys.stdout.write(CSI + "2J" + CSI + "H")
    sys.stdout.flush()

def bar10(norm):
    norm = 0.0 if norm is None else max(0.0, min(1.0, float(norm)))
    filled = int(round(norm * 10))
    return "█" * filled + "─" * (10 - filled)

# --- Device state ---
class DeviceState:
    def __init__(self, ip):
        self.ip = ip
        self.device_id = "N/A"
        self.api = "?"
        self.status = "Unknown"
        self.norm = None
        self.lux_est = None
        self.latency_ms = None
        self.last_error = None
        self.last_seen = None
        # playback
        self.playing = None
        self.play_until = None  # datetime or None
        self.queue_len = None

    def connection_state(self):
        if self.status.lower() == "offline":
            return "OFFLINE"
        if self.last_seen and (datetime.now(UTC) - self.last_seen > timedelta(seconds=STALE_AFTER_SEC)):
            return "STALE"
        return "ONLINE"

# --- Networking helpers ---
def timed_get(url, timeout):
    t0 = time.perf_counter()
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    rtt = int((time.perf_counter() - t0) * 1000)  # ms
    return r, rtt

def poll_once(state: DeviceState):
    base = f"http://{state.ip}"

    # /health
    try:
        r, rtt = timed_get(f"{base}/health", HTTP_TIMEOUT)
        j = r.json()
        state.device_id = j.get("device_id", state.device_id)
        state.api = j.get("api", state.api)
        state.status = j.get("status", "Unknown")
        state.latency_ms = rtt
        state.last_seen = datetime.now(UTC)
        state.last_error = None
    except requests.exceptions.RequestException as e:
        state.status = "Offline"
        state.latency_ms = None
        state.last_error = type(e).__name__
        return state

    # sensor
    try:
        r, _ = timed_get(f"{base}/sensor", HTTP_TIMEOUT)
        j = r.json()
        state.norm = j.get("norm")
        state.lux_est = j.get("lux_est")
    except requests.exceptions.RequestException as e:
        state.last_error = f"sensor:{type(e).__name__}"

    # playback
    try:
        r, _ = timed_get(f"{base}/playback", HTTP_TIMEOUT)
        if r.status_code == 200:
            j = r.json()
            state.playing = j.get("playing")
            until_ms = j.get("until_epoch_ms")
            state.queue_len = j.get("queue_len")
            state.play_until = (
                datetime.utcfromtimestamp(until_ms / 1000.0) if until_ms else None)
    except requests.exceptions.HTTPError as he:
        if getattr(he.response, "status_code", None) != 404:
            state.last_error = f"playback:{type(he).__name__}"
    except requests.exceptions.RequestException:
        pass

    return state

def CLI_db_print(state: DeviceState):
    clear()
    print(BOLD + "Pico Light Orchestra Dashboard" + RESET + "   (Ctrl+C to exit)")
    print("Polling: /health, /sensor, /playback ")
    print("-" * 90)
    print(BOLD + f"{'IP':<16} {'Device ID':<22} {'State':<10} {'RTT':<8} {'Light':<14} {'Playback':<22}" + RESET)
    print("-" * 90)

    conn = state.connection_state()
    state_txt = conn
    degraded = (state.last_error and conn != "OFFLINE")
    if degraded:
        state_txt = "DEGRADED"

    # Color for state
    if state_txt == "OFFLINE":
        state_fmt = color("OFFLINE", RED)
    elif state_txt in ("DEGRADED", "STALE"):
        state_fmt = color(state_txt, YELLOW)
    else:
        state_fmt = color("ONLINE", GREEN)

    # Return trip latency 
    if state.latency_ms is None:
        rtt = "--"
    else:
        rtt = f"{state.latency_ms}ms"
        if state.latency_ms >= LATENCY_WARN_MS:
            rtt += color(" (high)", YELLOW)

    # Light
    bar = bar10(state.norm)
    light_val = "n/a" if state.norm is None else f"{state.norm:.2f}"

    # Playback
    now = datetime.now(UTC)
    pb = "unknown"
    if state.playing is True:
        if state.play_until:
            remain = int((state.play_until - now).total_seconds() * 1000)
            pb = f"PLAYING {max(0, remain)}ms left"
        else:
            pb = "PLAYING"
    elif state.playing is False:
        pb = f"IDLE" + (f" (q={state.queue_len})" if state.queue_len else "")

    print(f"{state.ip:<16} {state.device_id:<22} {state_fmt:<10} {rtt:<8} "
          f"[{bar}] {light_val:<5} {pb:<22}")

    print("-" * 90)

    # Alerts
    alerts = []
    if state_txt == "OFFLINE":
        alerts.append(f"{state.ip} offline ({state.last_error})")
    elif state.latency_ms is not None and state.latency_ms >= LATENCY_WARN_MS:
        alerts.append(f"{state.ip} high latency {state.latency_ms}ms")
    elif degraded:
        alerts.append(f"{state.ip} degraded: {state.last_error}")

    if alerts:
        print(color("Alerts:", BOLD))
        for a in alerts:
            print(color(" - " + a, YELLOW if "latency" in a or "degraded" in a else RED))

if __name__ == "__main__":
    try:
        if not PICO_IP:
            print("No device configured. Set PICO_IP."); sys.exit(1)
        state = DeviceState(PICO_IP)
        while True:
            poll_once(state)
            CLI_db_print(state)
            time.sleep(max(0.05, POLL_INTERVAL))

    # Handle exit and events
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
