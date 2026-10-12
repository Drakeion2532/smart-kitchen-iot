/*
 * Smart Kitchen – ESP8266 Wi-Fi gateway
 * Reads JSON lines from the Arduino Uno and POSTs them to the backend
 * (kitchen/api.py  ->  POST /api/telemetry). Commands returned by the server
 * (e.g. {"commands":["FAN:1"]}) are forwarded back to the Uno.
 *
 * Board: NodeMCU / ESP-01 (ESP8266 core for Arduino)
 * Wiring: ESP RX <- Uno D12 (through 5V->3.3V divider), ESP TX -> Uno D11
 */
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClientSecure.h>

const char* WIFI_SSID = "YOUR_WIFI";
const char* WIFI_PASS = "YOUR_PASSWORD";
const char* API_URL   = "http://192.168.1.10:8000/api/telemetry";  // backend address
const char* DEVICE_ID = "kitchen-01";
const char* API_KEY   = "change-me";   // must match KITCHEN_API_KEY on the server

String line;

void setup() {
  Serial.begin(9600);            // hardware UART to the Uno
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
}

void forward(const String& json) {
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); return; }
  WiFiClient client;
  HTTPClient http;
  http.begin(client, API_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Id", DEVICE_ID);
  http.addHeader("X-Api-Key", API_KEY);
  int code = http.POST(json);
  if (code == 200) {
    String resp = http.getString();
    // very small parser: look for FAN:1 / FAN:0 / BUZZ:0 tokens
    if (resp.indexOf("FAN:1") >= 0) Serial.println("FAN:1");
    if (resp.indexOf("FAN:0") >= 0) Serial.println("FAN:0");
    if (resp.indexOf("BUZZ:0") >= 0) Serial.println("BUZZ:0");
  }
  http.end();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      line.trim();
      if (line.startsWith("{")) forward(line);
      line = "";
    } else if (line.length() < 300) {
      line += c;
    }
  }
}
