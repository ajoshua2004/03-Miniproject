# main.py for Raspberry Pi Pico W
# Title: Pico Light Orchestra Instrument Code

import machine
import time
import network
import json
import asyncio
import ubinascii  # Added for MAC address handling

# --- Pin Configuration ---
# The photosensor is connected to an Analog-to-Digital Converter (ADC) pin.
# We will read the voltage, which changes based on light.
photo_sensor_pin = machine.ADC(28)

# The buzzer is connected to a GPIO pin that supports Pulse Width Modulation (PWM).
# PWM allows us to create a square wave at a specific frequency to make a sound.
buzzer_pin = machine.PWM(machine.Pin(16))

# --- Global State ---
# This variable will hold the task that plays a note from an API call.
# This allows us to cancel it if a /stop request comes in.
api_note_task = None

API_VERSION = "1.0.0"
alpha = 0.2
filtered = 0


#---Dictionary of Notes---

# --- Core Functions ---
NOTES = {
    "C0": 16,   "C#0": 17,  "D0": 18,   "D#0": 19,  "E0": 21,   "F0": 22,   "F#0": 23,  "G0": 25,
    "G#0": 26,  "A0": 28,   "A#0": 29,  "B0": 31,

    "C1": 33,   "C#1": 35,  "D1": 37,   "D#1": 39,  "E1": 41,   "F1": 44,   "F#1": 46,  "G1": 49,
    "G#1": 52,  "A1": 55,   "A#1": 58,  "B1": 62,

    "C2": 65,   "C#2": 69,  "D2": 73,   "D#2": 78,  "E2": 82,   "F2": 87,   "F#2": 93,  "G2": 98,
    "G#2": 104, "A2": 110,  "A#2": 117, "B2": 123,

    "C3": 131,  "C#3": 139, "D3": 147,  "D#3": 156, "E3": 165,  "F3": 175,  "F#3": 185, "G3": 196,
    "G#3": 208, "A3": 220,  "A#3": 233, "B3": 247,

    "C4": 261,  "C#4": 277, "D4": 293,  "D#4": 311, "E4": 329,  "F4": 349,  "F#4": 370, "G4": 392,
    "G#4": 415, "A4": 440,  "A#4": 466, "B4": 494,

    "C5": 523,  "C#5": 554, "D5": 587,  "D#5": 622, "E5": 659,  "F5": 698,  "F#5": 740, "G5": 784,
    "G#5": 831, "A5": 880,  "A#5": 932, "B5": 988,

    "C6": 1047, "C#6": 1109,"D6": 1175, "D#6": 1245,"E6": 1319, "F6": 1397, "F#6": 1480,"G6": 1568,
    "G#6": 1661,"A6": 1760, "A#6": 1865,"B6": 1976,

    "C7": 2093, "C#7": 2217,"D7": 2349, "D#7": 2489,"E7": 2637, "F7": 2794, "F#7": 2960,"G7": 3136,
    "G#7": 3322,"A7": 3520, "A#7": 3729,"B7": 3951,

    "C8": 4186
}

def get_device_id():
    """Generate unique device_id based on MAC address."""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    mac = wlan.config('mac')
    mac_str = ubinascii.hexlify(mac).decode('utf-8').upper()
    return f"pico-w-{mac_str}"

def filter_value(new: float, old: float) -> float:
    """Exponential smoothing filter for stability."""
    return alpha * new + (1 - alpha) * old

def connect_to_wifi(wifi_config: str = "wifi_config.json"):
    """Connects the Pico W to the specified Wi-Fi network.

    This expects a JSON text file 'wifi_config.json' with 'ssid' and 'password' keys,
    which would look like
    {
        "ssid": "your_wifi_ssid",
        "password": "your_wifi_password"
    }
    """

    with open(wifi_config, "r") as f:
        data = json.load(f)
    ssid = data.get("ssid")
    password = data.get("password")
    print(f"SSID from JSON: '{ssid}'")
    print(f"Password from JSON: '{password}'")
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(data["ssid"], data["password"])
    print(f"Connecting to Wi-Fi SSID: {data['ssid']} ...")
    # Wait for connection or fail
    max_wait = 20
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

def map_volume(
    light_value,
    min_light=16000,
    max_light=32000,
    max_duty_u16=32768,
    louder_when="bright",   # "bright" or "dark"
    gamma=2.0               # >1 = more change at bright end; <1 = more at dark end
):
    """
    Map (min_light..max_light) -> duty (0..32768) with gamma shaping.
    louder_when:
      - "bright": louder as it gets brighter
      - "dark"  : louder as it gets darker (inverted)
    gamma:
      - >1 emphasizes changes at the high end (bright)
      - <1 emphasizes changes at the low end (dark)
    """
    # Clamp
    if light_value < min_light:
        light_value = min_light
    elif light_value > max_light:
        light_value = max_light

    span = max_light - min_light
    if span <= 0:
        return 0

    # Normalize so that 0 = darkest in range, 1 = brightest in range
    norm = (light_value - min_light) / span         # increases with 'light_value'
    brightness = 1.0 - norm                         # 0=dark, 1=bright (handles wiring where dark -> higher ADC)

    # Gamma shaping (exponential)
    if gamma <= 0:
        gamma = 1.0
    brightness_shaped = brightness ** gamma

    # Map to duty depending on desired direction
    if louder_when == "bright":
        duty = int(brightness_shaped * max_duty_u16)
    else:  # louder_when == "dark"
        duty = int((1.0 - brightness_shaped) * max_duty_u16)

    # Safety clamp
    if duty < 0:
        duty = 0
    elif duty > max_duty_u16:
        duty = max_duty_u16

    return duty


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
    buzzer_pin.duty_u16(0)  # 0% duty cycle means silence

def play_song(song):
    """Calls play tone with note dictionary to play a song"""
    for note,duration in song:
        frequency=NOTES.get(note,0)
        play_tone(frequency,duration)
        time.sleep_ms(50)
        
async def play_api_note(frequency, duration_s):
    """Coroutine to play a note from an API call, can be cancelled."""
    try:
        print(f"API playing note: {frequency}Hz for {duration_s}s")
        buzzer_pin.freq(int(frequency))
        buzzer_pin.duty_u16(32768)  # 50% duty cycle
        await asyncio.sleep(duration_s)
        stop_tone()
        print("API note finished.")
    except asyncio.CancelledError:
        stop_tone()
        print("API note cancelled.")


def map_value(x, in_min, in_max, out_min, out_max):
    """Maps a value from one range to another."""
    return (x - in_min) * (out_max - out_min) // (in_max - in_min) + out_min


async def handle_request(reader, writer):
    """Handles incoming HTTP requests."""
    global api_note_task

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

    # --- API Endpoint Routing ---
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
        
    elif method == "GET" and url == "/sensor":
        # Calculate sensor values
        raw_value = light_value  # Already read above
        norm_value = raw_value / 65535.0  # Normalize to 0.0-1.0
        
        # Simple lux estimation (this is approximate)
        # In bright sunlight: ~65000 ADC = ~1000 lux
        # In office lighting: ~30000 ADC = ~300 lux  
        # In dim room: ~5000 ADC = ~50 lux
        lux_est = (norm_value * 1000.0)  # Simple linear mapping
        
        response = json.dumps({
            "raw": raw_value,
            "norm": round(norm_value, 3),
            "lux_est": round(lux_est, 1)
        })
        content_type = "application/json"
        
    elif method == "GET" and url == "/health":
        response = json.dumps({
            "status": "ok",
            "device_id": get_device_id(),
            "api": API_VERSION
        })
        content_type = "application/json"
        
    elif method == "POST" and url == "/play_note":
        # This requires reading the request body, which is not trivial.
        # A simple approach for a known content length:
        # Note: A robust server would parse Content-Length header.
        #For this student project, we'll assume a small, simple JSON body.
        raw_data = await reader.read(1024)
        try:
            data = json.loads(raw_data)
            freq = data.get("frequency", 0)
            duration = data.get("duration", 0)

            # If a note is already playing via API, cancel it first
            if api_note_task:
                api_note_task.cancel()

            # Start the new note as a background task
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
        stop_tone()  # Force immediate stop
        response = '{"status": "ok", "message": "All sounds stopped."}'
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


async def main():
#     """Main execution loop."""
#     try:
#         ip = connect_to_wifi()
#         print(f"Starting web server on {ip}...")
#         asyncio.create_task(asyncio.start_server(handle_request, "0.0.0.0", 80))
#     except Exception as e:
#         print(f"Failed to initialize: {e}")
#         return

    # This loop runs the "default" behavior: playing sound based on light
#     while True:
#         # Only run this loop if no API note is currently scheduled to play
#         if api_note_task is None or api_note_task.done():
#             # Read the sensor. Values range from ~500 (dark) to ~65535 (bright)
#             light_value = photo_sensor_pin.read_u16()
# 
#             # Map the light value to a frequency range (e.g., C4 to C6)
#             # Adjust the input range based on your room's lighting
#             min_light = 1000
#             max_light = 65000
#             min_freq = 261  # C4
#             max_freq = 1046  # C6
#             is_dark=40000 #defines the darkness threshold 
#             # Clamp the light value to the expected range
#             clamped_light = max(min_light, min(light_value, max_light))
#             
#             if clamped_light<is_dark:
#                 frequency = map_value(
#                     clamped_light, min_light, max_light, min_freq, max_freq
#                 )
#                 
#              #   buzzer_pin.freq(clamped_light)
#                 buzzer_pin.freq(700)
#                 volume=map_volume(clamped_light)
#                 print (clamped_light)
#                 print(volume)
#                 buzzer_pin.duty_u16(volume)  # 50% duty cycle
#             else:
#                 stop_tone()  # If it's very dark, be quiet
# 
#         await asyncio.sleep_ms(50)  # type: ignore[attr-defined]
    JINGLE_BELLS = [
        # "Jingle bells, jingle bells, jingle all the way"
#         ("E4", 400), ("E4", 400), ("E4", 800),
#         ("E4", 400), ("E4", 400), ("E4", 800),
#         ("E4", 400), ("G4", 400), ("C4", 400), ("D4", 400), ("E4", 1200),
# 
#         # "Oh what fun it is to ride in a one horse open sleigh"
        ("F4", 400), ("F4", 400), ("F4", 400), ("F4", 400),#ends it
        ("F4", 400), ("E4", 400), ("E4", 400), ("E4", 400), ("E4", 400),# ends at a
        ("F4", 400),("F4", 400), ("D4", 400), ("B4", 400), ("C5", 800)
    ]



    play_song(JINGLE_BELLS)

# Run the main event loop
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Program stopped.")
        stop_tone()
