# main.py for Raspberry Pi Pico W
# Title: Pico Light Orchestra Instrument Code

import machine
import time
import network
import json
import asyncio

# --- Pin Configuration ---
photo_sensor_pin = machine.ADC(26)   # Use ADC(26) if wired to GP26, change to ADC(28) if using GP28
buzzer_pin = machine.PWM(machine.Pin(18))

# --- Global State ---
api_note_task = None
alpha = 0.2   # smoothing factor
filtered = 0  # last filtered value

# --- Sensor Helpers ---
def estimate_lux(raw: int) -> float:
    """Convert raw ADC reading to an approximate lux value."""
    if raw == 0:
        return 0.0
    RL = (10000 * (65535 - raw)) / raw   # photoresistor resistance (Ω)
    lux = 500 / (RL / 1000)              # rough lux estimate
    return lux

def filter_value(new: float, old: float) -> float:
    """Exponential smoothing filter for stability."""
    return alpha * new + (1 - alpha) * old

# --- Wi-Fi Setup ---
def connect_to_wifi(wifi_config: str = "wifi_config.json"):
    """Connects the Pico W to the specified Wi-Fi network."""
    with open(wifi_config, "r") as f:
        data = json.load(f)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(data["ssid"], data["password"])

    # Wait for connection or fail
    max_wait = 15
    print("Connecting to Wi-Fi...")
    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        max_wait -= 1
        time.sleep(1)

    if wlan.status() != 3:
        raise RuntimeError("Network connection failed")
    else:
        status = wlan.ifconfig()
        ip_address = status[0]
        print(f"Connected! Pico IP Address: {ip_address}")
    return ip_address

# --- Buzzer Helpers ---
def play_tone(frequency: int, duration_ms: int) -> None:
    """Plays a tone on the buzzer for a given duration."""
    if frequency > 0:
        buzzer_pin.freq(int(frequency))
        buzzer_pin.duty_u16(32768)  # 50% duty cycle
        time.sleep_ms(duration_ms)  # type: ignore[attr-defined]
        stop_tone()
    else:
        time.sleep_ms(duration_ms)  # type: ignore[attr-defined]

def stop_tone():
    """Stops any sound from playing."""
    buzzer_pin.duty_u16(0)

async def play_api_note(frequency, duration_s):
    """Coroutine to play a note from an API call, can be cancelled."""
    try:
        print(f"API playing note: {frequency}Hz for {duration_s}s")
        buzzer_pin.freq(int(frequency))
        buzzer_pin.duty_u16(32768)
        await asyncio.sleep(duration_s)
        stop_tone()
        print("API note finished.")
    except asyncio.CancelledError:
        stop_tone()
        print("API note cancelled.")

# --- Utility ---
def map_value(x, in_min, in_max, out_min, out_max):
    return (x - in_min) * (out_max - out_min) // (in_max - in_min) + out_min

# --- HTTP Request Handler ---
async def handle_request(reader, writer):
    global api_note_task, filtered

    print("Client connected")
    request_line = await reader.readline()
    # Skip headers
    while await reader.readline() != b"\r\n":
        pass

    try:
        request = str(request_line, "utf-8")
        method, url, _ = request.split()
        print(f"Request: {method} {url}")
    except (ValueError, IndexError):
        writer.write(b"HTTP/1.0 400 Bad Request\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    # Read current sensor value
    light_value = photo_sensor_pin.read_u16()

    response = ""
    content_type = "text/html"

    # Routes
    if method == "GET" and url == "/":
        html = f"""
        <html>
            <body>
                <h1>Pico Light Orchestra</h1>
                <p>Current light sensor reading: {light_value}</p>
            </body>
        </html>
        """
        response = html

    elif method == "POST" and url == "/play_note":
        raw_data = await reader.read(1024)
        try:
            data = json.loads(raw_data)
            freq = data.get("frequency", 0)
            duration = data.get("duration", 0)

            if api_note_task:
                api_note_task.cancel()

            api_note_task = asyncio.create_task(play_api_note(freq, duration))

            response = '{"status": "ok", "message": "Note playing started."}'
            content_type = "application/json"
        except (ValueError, json.JSONDecodeError):
            writer.write(b'HTTP/1.0 400 Bad Request\r\n\r\n{"error": "Invalid JSON"}\r\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

    elif method == "POST" and url == "/stop":
        if api_note_task:
            api_note_task.cancel()
            api_note_task = None
        stop_tone()
        response = '{"status": "ok", "message": "All sounds stopped."}'
        content_type = "application/json"

    elif method == "GET" and url == "/sensor":
        raw = photo_sensor_pin.read_u16()
        norm = raw / 65535
        lux = estimate_lux(raw)
        filtered = filter_value(norm, filtered)

        response = json.dumps({
            "raw": raw,
            "norm": round(norm, 4),
            "lux_est": round(lux, 2),
            "filtered_norm": round(filtered, 4)
        })
        content_type = "application/json"

    else:
        writer.write(b"HTTP/1.0 404 Not Found\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        return

    # Send response
    writer.write(
        f"HTTP/1.0 200 OK\r\nContent-type: {content_type}\r\n\r\n".encode("utf-8")
    )
    writer.write(response.encode("utf-8"))
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    print("Client disconnected")

# --- Main ---
async def main():
    try:
        ip = connect_to_wifi()
        print(f"Starting web server on {ip}...")
        server = await asyncio.start_server(handle_request, "0.0.0.0", 80)
        print("Web server started.")
    except Exception as e:
        print(f"Failed to initialize: {e}")
        return

    # Loop for default behavior (sound based on light)
    while True:
        if api_note_task is None or api_note_task.done():
            light_value = photo_sensor_pin.read_u16()
            min_light, max_light = 1000, 65000
            min_freq, max_freq = 261, 1046
            clamped_light = max(min_light, min(light_value, max_light))

            if clamped_light > min_light:
                frequency = map_value(clamped_light, min_light, max_light, min_freq, max_freq)
                buzzer_pin.freq(frequency)
                buzzer_pin.duty_u16(32768)
            else:
                stop_tone()
        await asyncio.sleep_ms(50)

# --- Run ---
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Program stopped.")
        stop_tone()
