# dashboard.py
# To be run on a student's computer (not the Pico)

import argparse
import ipaddress
import requests
import time
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# --- Configuration ---

PICO_IPS = [
    "192.168.1.101",
]

POLL_INTERVAL_SEC = 1.0
REQUEST_TIMEOUT_SEC = 1.0
MAX_WORKERS = 32
SPARKLINE_LEN = 16
LATENCY_WARN_MS = 150  # round-trip latency
OFFLINE_GRACE_SEC = 5

def color(text, c):
    return f"{c}{text}{RESET}"

def clear_screen():
    sys.stdout.write(CSI + "2J" + CSI + "H")
    sys.stdout.flush()

def bar10(norm):
    norm = 0.0 if norm is None else max(0.0, min(1.0, float(norm)))
    filled = int(round(norm * 10))
    return "█" * filled + "─" * (10 - filled)

def get_device_status(ip):
    """Fetches /health and /sensor data from a single device."""
    status = {"ip": ip, "device_id": "N/A", "status": "Error", "norm": 0.0}
    try:
        # Get health status
        health_res = requests.get(f"http://{ip}/health", timeout=1)
        health_res.raise_for_status()
        health_data = health_res.json()
        status.update(health_data)
        status["status"] = health_data.get("status", "Unknown")

        # Get sensor data
        sensor_res = requests.get(f"http://{ip}/sensor", timeout=1)
        sensor_res.raise_for_status()
        sensor_data = sensor_res.json()
        status["norm"] = sensor_data.get("norm", 0.0)

    except requests.exceptions.RequestException as e:
        status["status"] = f"Offline ({type(e).__name__})"

    return status


def render_dashboard(statuses):
    """Renders the collected statuses to the console."""

    print("--- Pico Orchestra Dashboard --- (Press Ctrl+C to exit)")
    print("-" * 60)
    print(f"{'IP Address':<16} {'Device ID':<25} {'Status':<10} {'Light Level':<20}")
    print("-" * 60)

    for status in statuses:
        # Create a simple bar graph for the light level
        norm = status.get("norm", 0.0)

        print(
            f"{status['ip']:<16} {status['device_id']:<25} {status['status'].capitalize():<10} "
            f"[{bar10(norm)}] {norm:.2f}"
        )

    print("-" * 60)


if __name__ == "__main__":
    try:
        while True:
            all_statuses = [get_device_status(ip) for ip in PICO_IPS]
            render_dashboard(all_statuses)
            time.sleep(1)  # Refresh every second

    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
