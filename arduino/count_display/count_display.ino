// Shows the BLE device count on the UNO Q's built-in 8x13 LED matrix.
//
// The Linux side (ble_scanner/scan.py --display) calls the RPC method
// "show_count" through the arduino-router with one integer argument.
// Numbers 0-999 are drawn with a 3x5 font; larger values show 999.
// The bottom-right pixel toggles on every update so you can see it's alive.

#include <Arduino_RouterBridge.h>
#include "Arduino_LED_Matrix.h"

Arduino_LED_Matrix matrix;

const int ROWS = 8;
const int COLS = 13;
const uint8_t ON = 7;  // brightness (0-7)

// 3x5 digit font: 5 rows per digit, low 3 bits are the pixels (MSB = left).
const uint8_t FONT[10][5] = {
  {7, 5, 5, 5, 7}, {2, 6, 2, 2, 7}, {7, 1, 7, 4, 7}, {7, 1, 7, 1, 7},
  {5, 5, 7, 1, 1}, {7, 4, 7, 1, 7}, {7, 4, 7, 5, 7}, {7, 1, 1, 1, 1},
  {7, 5, 7, 5, 7}, {7, 5, 7, 1, 7},
};

uint8_t frame[ROWS * COLS];
volatile int pendingCount = -1;
bool heartbeat = false;

void drawDigit(int digit, int col0, int row0) {
  for (int r = 0; r < 5; r++)
    for (int c = 0; c < 3; c++)
      if (FONT[digit][r] & (4 >> c)) frame[(row0 + r) * COLS + col0 + c] = ON;
}

void drawNumber(int n) {
  if (n < 0) n = 0;
  if (n > 999) n = 999;
  memset(frame, 0, sizeof(frame));

  char digits[4];
  int len = snprintf(digits, sizeof(digits), "%d", n);
  int width = len * 3 + (len - 1);   // 3 px per digit + 1 px gaps
  int col0 = (COLS - width) / 2;
  for (int i = 0; i < len; i++) drawDigit(digits[i] - '0', col0 + i * 4, 1);

  heartbeat = !heartbeat;
  if (heartbeat) frame[ROWS * COLS - 1] = ON;
  matrix.draw(frame);
}

// RPC handler, called from the Linux side. Only stores the value;
// drawing happens in loop().
int showCount(int n) {
  pendingCount = n;
  return n;
}

void setup() {
  matrix.begin();
  matrix.setGrayscaleBits(3);  // values 0-7
  memset(frame, 0, sizeof(frame));
  frame[3 * COLS + 6] = ON;    // single dot = waiting for first count
  matrix.draw(frame);

  Bridge.begin();
  Bridge.provide_safe("show_count", showCount);
}

void loop() {
  if (pendingCount >= 0) {
    int n = pendingCount;
    pendingCount = -1;
    drawNumber(n);
  }
  delay(20);
}
