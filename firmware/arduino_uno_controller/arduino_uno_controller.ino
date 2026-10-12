/*
 * Smart Kitchen – Arduino Uno sensor controller
 * ------------------------------------------------
 * Paper: "IoT-Powered Smart Kitchen Systems for Enhanced Efficiency and Safety"
 *        (Gaikwad, Sahani, Khan, Sakpal, Punde, Shinde – VIT Pune)
 *
 * Sensors : DHT11 (temp/humidity), MQ-2 (LPG/smoke), MQ-135 (air quality),
 *           HC-SR501 PIR (presence), load cell + HX711 (smart container),
 *           flame sensor (IR, digital)
 * Actuators: buzzer, red/green LED, exhaust-fan relay
 * Uplink  : one JSON line per second over SoftwareSerial to the ESP8266 gateway
 *
 * Local safety logic runs HERE, so alarms and the fan still work if Wi-Fi or
 * the cloud is down. The cloud only adds notifications + ML.
 *
 * Libraries: DHT sensor library (Adafruit), HX711 (bogde), ArduinoJson 6
 */
#include <DHT.h>
#include <HX711.h>
#include <SoftwareSerial.h>
#include <ArduinoJson.h>

// ---------- pins ----------
#define PIN_DHT        2
#define PIN_PIR        3
#define PIN_FLAME      4      // LOW = flame detected on most IR flame modules
#define PIN_HX_DOUT    5
#define PIN_HX_SCK     6
#define PIN_BUZZER     7
#define PIN_LED_RED    8
#define PIN_LED_GREEN  9
#define PIN_FAN_RELAY  10
#define PIN_ESP_RX     11     // Uno RX  <- ESP TX
#define PIN_ESP_TX     12     // Uno TX  -> ESP RX (use a 5V->3.3V divider!)
#define PIN_MQ2        A0
#define PIN_MQ135      A1

// ---------- thresholds (calibrate per kitchen) ----------
const float LPG_WARN_PPM   = 1000;   // MQ-2, LPG lower explosive limit is ~21,000 ppm
const float LPG_ALARM_PPM  = 2000;
const float AQ_BAD_PPM     = 1000;   // MQ-135 CO2-equivalent
const float TEMP_HIGH_C    = 45;
const unsigned long UNATTENDED_MS = 5UL * 60UL * 1000UL;   // stove heat with nobody present for 5 min

// MQ sensor curve constants (log-log fit from the datasheets): ppm = a * (Rs/R0)^b
const float MQ2_A = 574.25, MQ2_B = -2.222;   // LPG
const float MQ135_A = 110.47, MQ135_B = -2.862; // CO2
const float RL_KOHM = 10.0;
float mq2_r0 = 9.83, mq135_r0 = 76.63;      // overwritten by calibrate()

DHT dht(PIN_DHT, DHT11);
HX711 scale;
SoftwareSerial esp(PIN_ESP_RX, PIN_ESP_TX);

const float SCALE_FACTOR = 420.0;  // raw units per gram – calibrate with a known weight
unsigned long lastMotionMs = 0, lastSendMs = 0;
bool fanOverride = false;

float rsFromAdc(int adc) {
  float v = adc * (5.0 / 1023.0);
  return (5.0 - v) * RL_KOHM / max(v, 0.01f);
}

float ppm(int pin, float r0, float a, float b) {
  return a * pow(rsFromAdc(analogRead(pin)) / r0, b);
}

void calibrate() {
  // in clean air: Rs/R0 = 9.83 for MQ-2, 3.6 for MQ-135
  float s2 = 0, s135 = 0;
  for (int i = 0; i < 50; i++) { s2 += rsFromAdc(analogRead(PIN_MQ2)); s135 += rsFromAdc(analogRead(PIN_MQ135)); delay(100); }
  mq2_r0 = (s2 / 50) / 9.83;
  mq135_r0 = (s135 / 50) / 3.6;
}

void setup() {
  Serial.begin(9600);
  esp.begin(9600);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_FLAME, INPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_FAN_RELAY, OUTPUT);
  dht.begin();
  scale.begin(PIN_HX_DOUT, PIN_HX_SCK);
  scale.set_scale(SCALE_FACTOR);
  scale.tare();
  Serial.println(F("Warming MQ sensors (60 s)..."));
  delay(60000);
  calibrate();
  Serial.println(F("Ready"));
}

void handleCommand() {
  // ESP forwards cloud commands as single lines: FAN:1 / FAN:0 / BUZZ:0
  if (!esp.available()) return;
  String cmd = esp.readStringUntil('\n');
  cmd.trim();
  if (cmd == "FAN:1") fanOverride = true;
  else if (cmd == "FAN:0") fanOverride = false;
  else if (cmd == "BUZZ:0") digitalWrite(PIN_BUZZER, LOW);
}

void loop() {
  handleCommand();
  unsigned long now = millis();
  if (now - lastSendMs < 1000) return;
  lastSendMs = now;

  float t = dht.readTemperature();
  float h = dht.readHumidity();
  float lpg = ppm(PIN_MQ2, mq2_r0, MQ2_A, MQ2_B);
  float aq = ppm(PIN_MQ135, mq135_r0, MQ135_A, MQ135_B);
  bool motion = digitalRead(PIN_PIR) == HIGH;
  bool flame = digitalRead(PIN_FLAME) == LOW;
  float grams = scale.get_units(3);
  if (motion) lastMotionMs = now;

  // ---------- local safety rules ----------
  bool gasAlarm = lpg >= LPG_ALARM_PPM;
  bool gasWarn = lpg >= LPG_WARN_PPM;
  bool fire = flame || (t >= TEMP_HIGH_C && aq >= AQ_BAD_PPM);
  bool unattended = (t >= 35) && (now - lastMotionMs > UNATTENDED_MS);
  bool badAir = aq >= AQ_BAD_PPM;

  digitalWrite(PIN_FAN_RELAY, (gasWarn || badAir || fire || fanOverride) ? HIGH : LOW);
  digitalWrite(PIN_BUZZER, (gasAlarm || fire) ? HIGH : LOW);
  digitalWrite(PIN_LED_RED, (gasWarn || fire || unattended) ? HIGH : LOW);
  digitalWrite(PIN_LED_GREEN, (gasWarn || fire || unattended) ? LOW : HIGH);

  StaticJsonDocument<256> doc;
  doc["temp_c"] = t;
  doc["humidity"] = h;
  doc["lpg_ppm"] = lpg;
  doc["aq_ppm"] = aq;
  doc["motion"] = motion;
  doc["flame"] = flame;
  doc["container_g"] = grams;
  doc["fan"] = digitalRead(PIN_FAN_RELAY);
  doc["uptime_s"] = now / 1000;
  serializeJson(doc, esp);
  esp.println();
  serializeJson(doc, Serial);   // debug on USB
  Serial.println();
}
