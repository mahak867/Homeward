# HomeWard hardware: bedside sensor node (about ₹1,000)

A real bedside board that plugs straight into the running HomeWard platform:

- **Measures:** pulse and SpO₂ (MAX30102 finger sensor) and body temperature (DS18B20 probe).
- **Alerts in the room:** a buzzer and a red/green LED follow the patient's live alert state. Fast beeping means CRITICAL, a slow beep means HIGH, a chirp means WATCH, and green means all fine.
- **"I'm with the patient" button:** one press at the bedside acknowledges the open alerts and stops the escalation ladder. It is recorded in the audit log as *Bedside button*.
- **Works without internet:** if the platform is unreachable for 60 s, the board applies its own safety rules (SpO₂ ≤ 91, pulse ≥ 131 or ≤ 40 on three consecutive windows) and sounds the alarm locally. The LED turns amber to show it is offline.
- **Real sensor data is used directly:** the readings replace the simulated channels in that home's next readings, so they go through the same artifact gate, NEWS2, personal baseline, AI forecast and alerting. Lifting the finger off the sensor produces a real "check the sensor" artifact instead of a false alarm.

## Bill of materials

| Part | Approx. price (India) |
|---|---|
| ESP32 DevKit V1 (Wi-Fi) | ₹450 |
| MAX30102 pulse-oximeter module | ₹250 |
| DS18B20 waterproof temperature probe + 4.7 kΩ resistor | ₹130 |
| Active buzzer (3.3–5 V) | ₹20 |
| Red + green LED, 2 × 220 Ω resistors | ₹10 |
| Push button | ₹10 |
| Breadboard + jumper wires | ₹120 |
| **Total** | **≈ ₹990** |

Optional: an MLX90614 non-contact IR thermometer instead of the DS18B20 (₹900), or an Arduino Uno/Nano plus a laptop or Raspberry Pi over USB instead of the ESP32's Wi-Fi.

## Wiring

```
              ESP32 DevKit
           ┌──────────────────┐
  MAX30102 │ 3V3 ─── VIN      │
           │ GND ─── GND      │
           │ GPIO21 ─ SDA     │
           │ GPIO22 ─ SCL     │
           │                  │
  DS18B20  │ 3V3 ─── VDD ──┐  │
           │ GPIO4 ─ DATA ─┴ 4.7kΩ (pull-up to 3V3)
           │ GND ─── GND      │
           │                  │
  Buzzer   │ GPIO25 ─ (+)  (−) ─ GND
  LED red  │ GPIO26 ─ 220Ω ─ LED ─ GND
  LED green│ GPIO33 ─ 220Ω ─ LED ─ GND
  Button   │ GPIO27 ─ button ─ GND   (internal pull-up)
           └──────────────────┘
```

## Setup (about 10 minutes)

1. Run the platform so the board can reach it on your Wi-Fi:

   ```bash
   python run.py --host 0.0.0.0
   ```

   Note your laptop's IP address, for example `192.168.1.50`.

2. In the Arduino IDE, install the **ESP32 board package**. Then add these libraries: *SparkFun MAX3010x Pulse and Proximity Sensor Library*, *OneWire*, *DallasTemperature* and *ArduinoJson* (v7).

3. Open `hardware/esp32_homeward/esp32_homeward.ino`. Set `WIFI_SSID`, `WIFI_PASS`, `SERVER` (for example `http://192.168.1.50:8000`) and `PATIENT_ID` (`P-101` = Kamla Devi). Then upload to **ESP32 Dev Module**.

4. Put a finger on the sensor. Within about 10 s, the dashboard shows **"🔌 Real sensor"** for that patient and the charts use the real pulse, SpO₂ and temperature.

**No Wi-Fi?** Set `USE_WIFI false`, connect the board by USB, and run the bridge:

```bash
pip install pyserial
```

```bash
python hardware/serial_bridge.py --port /dev/ttyUSB0 --patient P-101
```

On Windows, use `--port COM3` instead.

## No board yet? Use the virtual ESP32

`virtual_esp32.py` uses exactly the same protocol as the real board, so the hardware path can be shown on any laptop:

```bash
python hardware/virtual_esp32.py --patient P-101
```

```bash
python hardware/virtual_esp32.py --patient P-101 --hypoxia
```

```bash
python hardware/virtual_esp32.py --patient P-101 --finger-off
```

```bash
python hardware/virtual_esp32.py --patient P-101 --press-button
```

- `--hypoxia` makes SpO₂ drift down until the buzzer goes to fast alarm.
- `--finger-off` produces a sensor artifact, so the system asks you to re-seat the probe instead of raising an alarm.
- `--press-button` acknowledges the open alerts from the "bedside".

## Device API

| Endpoint | Purpose |
|---|---|
| `POST /api/devices/{patient}/sensor` (header `X-Device-Key`) | Body: `{"hr", "spo2", "temp", "finger", "sqi", "button", "firmware"}`. The response includes the current alarm command. |
| `GET /api/devices/{patient}/command` | Returns `{"tier", "alarm": off/chirp/slow/fast, "led": green/amber/red, "open_alerts"}`. |
| `POST /api/ingest` | Full hub protocol: ordered, sequence-numbered batches of complete readings plus events (pill box, bed sensor, local alarm). |

## Extending the bedside kit

| Add-on | How it plugs in |
|---|---|
| Smart pill box | A reed switch on the lid sends a `PILLBOX_OPEN` event, which drives the *missed dose* alert. |
| Bed-occupancy sensor | An FSR or load cell under the mattress leg sends `BED_EXIT` / `BED_RETURN` events, which drive the *possible fall at night* alert. |
| Smart scale | An HX711 + load cell adds `weight` to the reading, giving the heart-failure fluid trend. |
| BP cuff | A BLE cuff read through the Raspberry Pi gateway adds `sbp`/`dbp`. |

The platform already handles all of these events and fields. The simulated hubs produce them today.
