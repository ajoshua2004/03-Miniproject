# dashboard.py
# To be run on a student's computer (not the Pico)

import requests
import time

# --- Configuration ---
# Students should populate this list with the IP address(es) of their Pico
PICO_IPS = [
    "172.20.10.8",
]


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
        light_level = status.get("norm", 0.0)
        bar_length = int(light_level * 10)
        bar = "█" * bar_length + "─" * (10 - bar_length)

        print(
            f"{status['ip']:<16} {status['device_id']:<25} {status['status'].capitalize():<10} "
            f"[{bar}] {light_level:.2f}"
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