/*
  HomeWard bedside sensor node - ESP32
  ------------------------------------
  Measures pulse + SpO2 (MAX30102) and body temperature (DS18B20 probe), pushes them to
  the HomeWard platform every ~5 s, and drives a buzzer + RGB/2-colour LED from the
  platform's alarm state. A push button lets the caregiver acknowledge alerts
  ("I'm with the patient") right at the bedside.

  Safety net: if the platform is unreachable for 60 s, the board keeps measuring and
  sounds its own alarm on dangerous readings (SpO2 <= 91, pulse >= 131 or <= 40 on
  3 consecutive valid windows) - the same "edge works without cloud" idea as the hub.

  Wiring (see hardware/README.md):
    MAX30102  VIN->3V3  GND->GND  SDA->GPIO21  SCL->GPIO22
    DS18B20   VDD->3V3  GND->GND  DATA->GPIO4 (+4.7k pull-up to 3V3)
    Buzzer    + ->GPIO25 (active buzzer)  - ->GND
    LED red   GPIO26 -220R-> LED -> GND      LED green GPIO33 -220R-> LED -> GND
    Button    GPIO27 -> button -> GND   (internal pull-up)

  Libraries (Arduino Library Manager):
    "SparkFun MAX3010x Pulse and Proximity Sensor Library", "OneWire",
    "DallasTemperature", "ArduinoJson" (v7)
  Board: "ESP32 Dev Module" (esp32 core 2.x or 3.x)

  Set USE_WIFI to false to use a USB cable instead: the board then prints one JSON line per
  measurement and hardware/serial_bridge.py forwards it (works with any Arduino too).
*/
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "MAX30105.h"
#include "spo2_algorithm.h"

// ---------------- configuration ----------------
#define USE_WIFI true
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASSWORD";
const char* SERVER      = "http://192.168.1.50:8000";   // laptop running `python run.py --host 0.0.0.0`
const char* PATIENT_ID  = "P-101";
const char* DEVICE_KEY  = "54d70ef3b87da2222177a434d28ffcd5";  // this board's own key: `python -m homeward.core HUB-P-101`
const char* FIRMWARE    = "homeward-esp32 1.0";

// ---------------- pins ----------------
const int PIN_ONEWIRE = 4, PIN_BUZZER = 25, PIN_LED_RED = 26, PIN_LED_GREEN = 33, PIN_BUTTON = 27;

MAX30105 particleSensor;
OneWire oneWire(PIN_ONEWIRE);
DallasTemperature tempSensor(&oneWire);

const int BUF = 100;                      // 4 s of data at 25 samples/s
uint32_t irBuf[BUF], redBuf[BUF];

volatile bool buttonPressed = false;
volatile int alarmPattern = 0;            // 0 off, 1 chirp, 2 slow, 3 fast
volatile int ledColour = 0;               // 0 green, 1 amber, 2 red
unsigned long lastServerOk = 0;
int dangerStreak = 0;
float hrHist[3] = {0}, spHist[3] = {0};
int histN = 0;

void IRAM_ATTR onButton() { buttonPressed = true; }

// Runs on core 0 so alarms keep sounding while core 1 is busy sampling.
void alarmTask(void*) {
  for (;;) {
    int p = alarmPattern;
    digitalWrite(PIN_LED_RED, ledColour >= 1);
    digitalWrite(PIN_LED_GREEN, ledColour <= 1);   // amber = red + green
    if (p == 3)      { digitalWrite(PIN_BUZZER, HIGH); vTaskDelay(pdMS_TO_TICKS(150)); digitalWrite(PIN_BUZZER, LOW); vTaskDelay(pdMS_TO_TICKS(150)); }
    else if (p == 2) { digitalWrite(PIN_BUZZER, HIGH); vTaskDelay(pdMS_TO_TICKS(400)); digitalWrite(PIN_BUZZER, LOW); vTaskDelay(pdMS_TO_TICKS(1600)); }
    else if (p == 1) { digitalWrite(PIN_BUZZER, HIGH); vTaskDelay(pdMS_TO_TICKS(60));  digitalWrite(PIN_BUZZER, LOW); vTaskDelay(pdMS_TO_TICKS(9940)); }
    else             { digitalWrite(PIN_BUZZER, LOW);  vTaskDelay(pdMS_TO_TICKS(200)); }
  }
}

float median3(float* a, int n) {
  float b[3]; for (int i = 0; i < n; i++) b[i] = a[i];
  for (int i = 0; i < n; i++) for (int j = i + 1; j < n; j++) if (b[j] < b[i]) { float t = b[i]; b[i] = b[j]; b[j] = t; }
  return b[n / 2];
}

void connectWifi() {
  if (!USE_WIFI || WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) delay(250);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUZZER, OUTPUT); pinMode(PIN_LED_RED, OUTPUT); pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_BUTTON), onButton, FALLING);
  xTaskCreatePinnedToCore(alarmTask, "alarm", 2048, nullptr, 1, nullptr, 0);

  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("{\"error\":\"MAX30102 not found - check wiring\"}");
    ledColour = 2; alarmPattern = 1;
  }
  // ledBrightness, sampleAverage, ledMode(2=red+IR), sampleRate, pulseWidth, adcRange
  particleSensor.setup(60, 4, 2, 100, 411, 4096);
  tempSensor.begin();
  connectWifi();
  lastServerOk = millis();
}

bool sampleWindow(int32_t& hr, int32_t& spo2, bool& finger) {
  uint64_t irSum = 0;
  for (int i = 0; i < BUF; i++) {
    while (!particleSensor.available()) particleSensor.check();
    redBuf[i] = particleSensor.getRed();
    irBuf[i] = particleSensor.getIR();
    irSum += irBuf[i];
    particleSensor.nextSample();
  }
  finger = (irSum / BUF) > 50000;          // no finger -> very low IR reflection
  int8_t spValid = 0, hrValid = 0;
  maxim_heart_rate_and_oxygen_saturation(irBuf, BUF, redBuf, &spo2, &spValid, &hr, &hrValid);
  return finger && spValid && hrValid && hr > 25 && hr < 230 && spo2 >= 50 && spo2 <= 100;
}

void loop() {
  int32_t hr = 0, spo2 = 0; bool finger = false;
  bool valid = sampleWindow(hr, spo2, finger);
  tempSensor.requestTemperatures();
  float temp = tempSensor.getTempCByIndex(0);
  bool tempOk = temp > 30 && temp < 43;

  if (valid) {                             // smooth with a running median of 3 windows
    hrHist[histN % 3] = hr; spHist[histN % 3] = spo2; histN++;
  }
  int n = histN < 3 ? histN : 3;
  float hrS = n ? median3(hrHist, n) : 0, spS = n ? median3(spHist, n) : 0;

  JsonDocument doc;
  if (valid && n) { doc["hr"] = hrS; doc["spo2"] = spS; doc["sqi"] = 0.9; }
  if (tempOk) doc["temp"] = round(temp * 10) / 10.0;
  doc["finger"] = finger;
  doc["firmware"] = FIRMWARE;
  if (buttonPressed) { doc["button"] = true; buttonPressed = false; }
  String body; serializeJson(doc, body);

  bool serverOk = false;
  if (USE_WIFI) {
    connectWifi();
    if (WiFi.status() == WL_CONNECTED) {
      HTTPClient http;
      http.setTimeout(3000);
      http.begin(String(SERVER) + "/api/devices/" + PATIENT_ID + "/sensor");
      http.addHeader("Content-Type", "application/json");
      http.addHeader("X-Device-Key", DEVICE_KEY);
      int code = http.POST(body);
      if (code == 200) {
        JsonDocument res;
        if (!deserializeJson(res, http.getString())) {
          String a = res["alarm"] | "off", led = res["led"] | "green";
          alarmPattern = a == "fast" ? 3 : a == "slow" ? 2 : a == "chirp" ? 1 : 0;
          ledColour = led == "red" ? 2 : led == "amber" ? 1 : 0;
          serverOk = true;
          lastServerOk = millis();
        }
      }
      http.end();
    }
  } else {
    Serial.println(body);                  // serial_bridge.py forwards this line...
    unsigned long t0 = millis();           // ...and answers with the alarm command
    while (millis() - t0 < 1500) {
      if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        JsonDocument res;
        if (!deserializeJson(res, line) && res["alarm"].is<const char*>()) {
          String a = res["alarm"] | "off", led = res["led"] | "green";
          alarmPattern = a == "fast" ? 3 : a == "slow" ? 2 : a == "chirp" ? 1 : 0;
          ledColour = led == "red" ? 2 : led == "amber" ? 1 : 0;
          serverOk = true; lastServerOk = millis();
          break;
        }
      }
      delay(10);
    }
  }

  // ---- edge safety net: platform unreachable for 60 s -> local rules
  if (!serverOk && millis() - lastServerOk > 60000) {
    bool danger = valid && (spS <= 91 || hrS >= 131 || hrS <= 40);
    dangerStreak = danger ? dangerStreak + 1 : 0;
    alarmPattern = dangerStreak >= 3 ? 3 : 0;
    ledColour = dangerStreak >= 3 ? 2 : 1;  // amber = offline
  }
  if (USE_WIFI) { Serial.print(serverOk ? "[ok] " : "[offline] "); Serial.println(body); }
}
