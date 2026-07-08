/*
  Minimal ESP32-C3 upload and USB-CDC serial check.

  Arduino IDE / arduino-cli board options:
    FQBN: esp32:esp32:esp32c3
    USB CDC On Boot: Enabled
*/

#include <Arduino.h>

#ifndef LED_BUILTIN
#define LED_BUILTIN 8
#endif

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
}

void loop() {
  static uint32_t count = 0;
  digitalWrite(LED_BUILTIN, count & 1u);
  Serial.printf("c3_serial_blink count=%lu ms=%lu\n",
                static_cast<unsigned long>(count),
                static_cast<unsigned long>(millis()));
  count++;
  delay(1000);
}
