/*
 * Arduino HID Gamepad Control - Leonardo/Pro Micro V1
 *
 * Companion to arduino_control_leonardo_v3.ino (mouse/keyboard). Same design rules:
 *   - No Arduino String. Fixed global buffer + avr-libc parsing (strtol/strchr).
 *     Zero heap allocation, so long uptime can't fragment the 2.5KB SRAM.
 *   - int16_t arithmetic where possible (AVR is a 16-bit machine; long math is
 *     emulated and costs several instructions per operation).
 *   - All Serial literals in PROGMEM via F().
 *   - Synchronous request/response: host sends a line, blocks on "OK".
 *
 * REPORT BATCHING: Joystick.begin(false) disables auto-send. Every handler mutates
 * local state and then calls Joystick.sendState() exactly once, so one command ==
 * one USB HID report. Without this, a full-state update would emit up to 7 reports
 * and the host would see torn intermediate stick positions.
 *
 * Requires: ArduinoJoystickLibrary by MHeironimus (Library Manager: "Joystick").
 * Compatible with: Arduino Leonardo, Pro Micro (ATmega32U4)
 *
 * === SERIAL PROTOCOL (115200 baud, newline-terminated) ===
 *   L<x>,<y>          Left stick.  x/y in -32767..32767
 *   R<x>,<y>          Right stick. x/y in -32767..32767
 *   T<lt>,<rt>        Triggers.    0..255 each
 *   V<dir>            D-pad/hat.   0..7 clockwise from up, -1 = centered
 *   B<n>              Button tap   (press, 50ms, release). n in 0..15
 *   D<n>              Button down (held until U)
 *   U<n>              Button up
 *   H<n>,<ms>         Button hold for ms
 *   G<lx>,<ly>,<rx>,<ry>,<lt>,<rt>,<hat>   Full state, one report. Main loop command.
 *   Z                 Reset everything to neutral (sticks centered, all released)
 *   P                 Ping -> PONG
 *
 * Replies: "OK", "PONG", or "ERROR:<reason>".
 */

#include <Joystick.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

// === CAPABILITY CONSTANTS ===
constexpr uint8_t BUTTON_COUNT = 16;
constexpr int16_t AXIS_MIN = -32767;
constexpr int16_t AXIS_MAX = 32767;
constexpr int16_t TRIGGER_MIN = 0;
constexpr int16_t TRIGGER_MAX = 255;

// === TIMING CONSTANTS ===
constexpr uint16_t DELAY_BUTTON_TAP = 50;   // Button press hold time (ms)

// === COMMAND BUFFER (fixed size - no heap allocation for parsing) ===
// Longest real command is G with six 6-char signed axes + hat: "G-32767,-32767,-32767,-32767,255,255,7"
constexpr uint8_t CMD_BUF_SIZE = 48;
static char cmdBuf[CMD_BUF_SIZE];

// Joystick_(reportId, type, buttonCount, hatSwitchCount,
//           X, Y, Z, Rx, Ry, Rz, Rudder, Throttle, Accelerator, Brake, Steering)
// Z = left trigger, Rz = right trigger (matches the usual XInput-style mapping).
Joystick_ Joystick(
  JOYSTICK_DEFAULT_REPORT_ID, JOYSTICK_TYPE_GAMEPAD,
  BUTTON_COUNT, 1,
  true, true,    // X, Y      - left stick
  true,          // Z         - left trigger
  true, true,    // Rx, Ry    - right stick
  true,          // Rz        - right trigger
  false, false, false, false, false
);

// === Forward declarations ===
static void trimTrailingWhitespace(char* buf, uint8_t len);
static int16_t clampAxis(long v, int16_t lo, int16_t hi);
static int8_t parseInts(char* s, long* out, uint8_t maxCount);
static void resetAll();
static void handleLeftStick(char* params);
static void handleRightStick(char* params);
static void handleTriggers(char* params);
static void handleHat(char* params);
static void handleButtonTap(char* params);
static void handleButtonDown(char* params);
static void handleButtonUp(char* params);
static void handleButtonHold(char* params);
static void handleFullState(char* params);
static void applyHat(long dir);

void setup() {
  Serial.begin(115200);

  Joystick.setXAxisRange(AXIS_MIN, AXIS_MAX);
  Joystick.setYAxisRange(AXIS_MIN, AXIS_MAX);
  Joystick.setRxAxisRange(AXIS_MIN, AXIS_MAX);
  Joystick.setRyAxisRange(AXIS_MIN, AXIS_MAX);
  Joystick.setZAxisRange(TRIGGER_MIN, TRIGGER_MAX);
  Joystick.setRzAxisRange(TRIGGER_MIN, TRIGGER_MAX);

  Joystick.begin(false);  // false = manual sendState(), see header

  while (!Serial) delay(10);

  resetAll();
  Serial.println(F("READY"));
}

void loop() {
  if (Serial.available() > 0) {
    size_t len = Serial.readBytesUntil('\n', cmdBuf, CMD_BUF_SIZE - 1);
    cmdBuf[len] = '\0';
    trimTrailingWhitespace(cmdBuf, (uint8_t)len);

    if (cmdBuf[0] == '\0') return;

    char cmd = cmdBuf[0];
    char* params = cmdBuf + 1;

    switch (cmd) {
      case 'L': handleLeftStick(params); break;
      case 'R': handleRightStick(params); break;
      case 'T': handleTriggers(params); break;
      case 'V': handleHat(params); break;
      case 'B': handleButtonTap(params); break;
      case 'D': handleButtonDown(params); break;
      case 'U': handleButtonUp(params); break;
      case 'H': handleButtonHold(params); break;
      case 'G': handleFullState(params); break;
      case 'Z':
        resetAll();
        Serial.println(F("OK"));
        break;
      case 'P': Serial.println(F("PONG")); break;
      default:
        Serial.println(F("ERROR:UNKNOWN_CMD"));
        break;
    }
  }
}

// === PARSING HELPERS ===

// Strips trailing whitespace in place. The paired host controller never sends
// leading whitespace, so leading-trim is intentionally not implemented.
static void trimTrailingWhitespace(char* buf, uint8_t len) {
  while (len > 0 && isspace((unsigned char)buf[len - 1])) {
    buf[--len] = '\0';
  }
}

// Clamps a long-range parsed value into range. Clamping happens before narrowing
// so an oversized/garbage value from a malformed command can't wrap.
static int16_t clampAxis(long v, int16_t lo, int16_t hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return (int16_t)v;
}

// Parses up to maxCount comma-separated integers into out[].
// Returns how many were parsed, or -1 if the first field isn't a number at all.
static int8_t parseInts(char* s, long* out, uint8_t maxCount) {
  uint8_t count = 0;
  char* p = s;

  while (count < maxCount) {
    char* endptr;
    long v = strtol(p, &endptr, 10);
    if (endptr == p) break;  // no digits consumed

    out[count++] = v;

    if (*endptr != ',') break;
    p = endptr + 1;
  }

  return count == 0 ? -1 : (int8_t)count;
}

// === STATE ===

static void resetAll() {
  Joystick.setXAxis(0);
  Joystick.setYAxis(0);
  Joystick.setRxAxis(0);
  Joystick.setRyAxis(0);
  Joystick.setZAxis(TRIGGER_MIN);
  Joystick.setRzAxis(TRIGGER_MIN);
  Joystick.setHatSwitch(0, JOYSTICK_HATSWITCH_RELEASE);

  for (uint8_t i = 0; i < BUTTON_COUNT; i++) {
    Joystick.setButton(i, 0);
  }

  Joystick.sendState();
}

// === AXES ===

static void handleLeftStick(char* params) {
  long v[2];
  if (parseInts(params, v, 2) != 2) {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }

  Joystick.setXAxis(clampAxis(v[0], AXIS_MIN, AXIS_MAX));
  Joystick.setYAxis(clampAxis(v[1], AXIS_MIN, AXIS_MAX));
  Joystick.sendState();

  Serial.println(F("OK"));
}

static void handleRightStick(char* params) {
  long v[2];
  if (parseInts(params, v, 2) != 2) {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }

  Joystick.setRxAxis(clampAxis(v[0], AXIS_MIN, AXIS_MAX));
  Joystick.setRyAxis(clampAxis(v[1], AXIS_MIN, AXIS_MAX));
  Joystick.sendState();

  Serial.println(F("OK"));
}

static void handleTriggers(char* params) {
  long v[2];
  if (parseInts(params, v, 2) != 2) {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }

  Joystick.setZAxis(clampAxis(v[0], TRIGGER_MIN, TRIGGER_MAX));
  Joystick.setRzAxis(clampAxis(v[1], TRIGGER_MIN, TRIGGER_MAX));
  Joystick.sendState();

  Serial.println(F("OK"));
}

// === HAT / D-PAD ===

// dir 0..7 clockwise from up (0=N, 1=NE, 2=E ... 7=NW). Anything else centers it.
// The library wants degrees, so the direction index is scaled by 45.
static void applyHat(long dir) {
  if (dir < 0 || dir > 7) {
    Joystick.setHatSwitch(0, JOYSTICK_HATSWITCH_RELEASE);
  } else {
    Joystick.setHatSwitch(0, (int16_t)(dir * 45));
  }
}

static void handleHat(char* params) {
  long v[1];
  if (parseInts(params, v, 1) != 1) {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }

  applyHat(v[0]);
  Joystick.sendState();

  Serial.println(F("OK"));
}

// === BUTTONS ===

static void handleButtonTap(char* params) {
  long v[1];
  if (parseInts(params, v, 1) != 1 || v[0] < 0 || v[0] >= BUTTON_COUNT) {
    Serial.println(F("ERROR:INVALID_BUTTON"));
    return;
  }

  uint8_t btn = (uint8_t)v[0];

  Joystick.setButton(btn, 1);
  Joystick.sendState();
  delay(DELAY_BUTTON_TAP);
  Joystick.setButton(btn, 0);
  Joystick.sendState();

  Serial.println(F("OK"));
}

static void handleButtonDown(char* params) {
  long v[1];
  if (parseInts(params, v, 1) != 1 || v[0] < 0 || v[0] >= BUTTON_COUNT) {
    Serial.println(F("ERROR:INVALID_BUTTON"));
    return;
  }

  Joystick.setButton((uint8_t)v[0], 1);
  Joystick.sendState();

  Serial.println(F("OK"));
}

static void handleButtonUp(char* params) {
  long v[1];
  if (parseInts(params, v, 1) != 1 || v[0] < 0 || v[0] >= BUTTON_COUNT) {
    Serial.println(F("ERROR:INVALID_BUTTON"));
    return;
  }

  Joystick.setButton((uint8_t)v[0], 0);
  Joystick.sendState();

  Serial.println(F("OK"));
}

static void handleButtonHold(char* params) {
  long v[2];
  if (parseInts(params, v, 2) != 2) {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }
  if (v[0] < 0 || v[0] >= BUTTON_COUNT) {
    Serial.println(F("ERROR:INVALID_BUTTON"));
    return;
  }

  uint8_t btn = (uint8_t)v[0];
  // Clamped to a positive int: delay() takes unsigned long, so a negative hold
  // value would otherwise wrap into a ~49-day freeze.
  int holdMs = (int)clampAxis(v[1], 0, 32767);

  Joystick.setButton(btn, 1);
  Joystick.sendState();
  delay(holdMs);
  Joystick.setButton(btn, 0);
  Joystick.sendState();

  Serial.println(F("OK"));
}

// === FULL STATE (single report - use this in a polling loop) ===

static void handleFullState(char* params) {
  long v[7];
  if (parseInts(params, v, 7) != 7) {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }

  Joystick.setXAxis(clampAxis(v[0], AXIS_MIN, AXIS_MAX));
  Joystick.setYAxis(clampAxis(v[1], AXIS_MIN, AXIS_MAX));
  Joystick.setRxAxis(clampAxis(v[2], AXIS_MIN, AXIS_MAX));
  Joystick.setRyAxis(clampAxis(v[3], AXIS_MIN, AXIS_MAX));
  Joystick.setZAxis(clampAxis(v[4], TRIGGER_MIN, TRIGGER_MAX));
  Joystick.setRzAxis(clampAxis(v[5], TRIGGER_MIN, TRIGGER_MAX));
  applyHat(v[6]);

  Joystick.sendState();  // one report for the whole frame

  Serial.println(F("OK"));
}
