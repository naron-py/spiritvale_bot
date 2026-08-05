/*
 * Arduino HID Control - Leonardo/Pro Micro V3 (OPTIMIZED)
 *
 * PERFORMANCE / RELIABILITY NOTES (this pass):
 * - Removed all Arduino String usage. On a 2.5KB-SRAM ATmega32U4, String's heap
 *   allocations fragment over long uptime (this firmware runs for hours at a time,
 *   dispatching a command on every mouse move/click) - a classic AVR reliability bug
 *   class. Command parsing now runs entirely on a fixed stack/global buffer with
 *   avr-libc string functions (strtol/strchr/strcmp) - zero heap allocation.
 * - Coordinates are now int16_t instead of long. Screen coords max out at 5119/1439,
 *   comfortably inside int16_t range; AVR does 16-bit math natively but emulates
 *   32-bit (long) math with multiple instructions, so this is a real CPU/Flash win
 *   on every move command, not just a style change.
 * - All Serial string literals moved to PROGMEM via F() - AVR otherwise copies every
 *   string literal into RAM at boot even though it's only ever read, not written.
 * - Timing (every delay()/delayMicroseconds() value and call order) is UNCHANGED.
 *   This is a synchronous request/response protocol (host sends command, blocks on
 *   the "OK" reply) - there is no concurrent work happening during these delays for
 *   millis()-based non-blocking scheduling to overlap with, so converting away from
 *   delay() would add complexity with no real benefit and risks changing the
 *   observed timing the game input relies on. Deliberately left as delay().
 *
 * Compatible with: Arduino Leonardo, Pro Micro (ATmega32U4)
 */

#include <Mouse.h>
#include <Keyboard.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

// === SCREEN / MOVEMENT CONSTANTS ===
constexpr int16_t SCREEN_WIDTH = 5120;
constexpr int16_t SCREEN_HEIGHT = 1440;
constexpr int8_t MAX_MOVE = 127;

// === TIMING CONSTANTS (for maintainability - values unchanged from original) ===
constexpr uint16_t DELAY_KEY_SETTLE = 10;     // Keyboard key settle time (ms)
constexpr uint16_t DELAY_CLICK_HOLD = 50;     // Mouse click hold time (ms)
constexpr uint16_t DELAY_COMBO_HOLD = 50;     // Combo (Alt+Click) hold time (ms)
constexpr uint16_t DELAY_CLEANUP = 10;        // Cleanup delay after release (ms)
constexpr uint16_t DELAY_PRE_CLICK_SETTLE = 10;  // Settle after move, before click (ms)
constexpr uint16_t STEP_PACING_US = 100;      // Pacing between relative move reports (us)

// === COMMAND BUFFER (fixed size - no heap allocation for parsing) ===
constexpr uint8_t CMD_BUF_SIZE = 32;  // longest real command ("KALT+CTRL+SHIFT+F12") fits with margin
static char cmdBuf[CMD_BUF_SIZE];

static int16_t current_x = 0;
static int16_t current_y = 0;

// === Forward declarations ===
static void trimTrailingWhitespace(char* buf, uint8_t len);
static int16_t clampCoord(long v, int16_t lo, int16_t hi);
static int8_t clampStep(int16_t v);
static void moveToAbsolute(int16_t target_x, int16_t target_y);
static void handleMoveAbsolute(char* params);
static void handleMoveAbsoluteAndClick(char* params);
static void handleClick(char* params);
static void handleHold(char* params);
static void handleMouseDown(char* params);
static void handleMouseUp(char* params);
static void handleKeyboard(char* params);
static void pressToken(const char* key, bool press);
static void handleKeyboardShortcut(char* params);
static void handleAltClick(char* params);
static void handleSync(char* params);

void setup() {
  Serial.begin(115200);
  Mouse.begin();
  Keyboard.begin();

  while (!Serial) delay(10);

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
      case 'M':
        if (params[0] == 'A') handleMoveAbsoluteAndClick(params + 1);
        else handleMoveAbsolute(params);
        break;
      case 'C': handleClick(params); break;
      case 'H': handleHold(params); break;
      case 'D': handleMouseDown(params); break;
      case 'U': handleMouseUp(params); break;
      case 'K': handleKeyboard(params); break;
      case 'A': handleAltClick(params); break;
      case 'S': handleSync(params); break;
      case 'P': Serial.println(F("PONG")); break;
      case 'R':
        current_x = 0;
        current_y = 0;
        Serial.println(F("OK"));
        break;
      default:
        Serial.println(F("ERROR:UNKNOWN_CMD"));
        break;
    }
  }
}

// === PARSING HELPERS ===

// Strips trailing whitespace in place (mirrors String::trim()'s trailing-side effect;
// the paired host controller never sends leading whitespace, so leading-trim is
// intentionally not implemented - see file header).
static void trimTrailingWhitespace(char* buf, uint8_t len) {
  while (len > 0 && isspace((unsigned char)buf[len - 1])) {
    buf[--len] = '\0';
  }
}

// Clamps a long-range parsed value into a screen-coordinate int16_t. Clamping happens
// before narrowing so an oversized/garbage value from a malformed command can't wrap.
static int16_t clampCoord(long v, int16_t lo, int16_t hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return (int16_t)v;
}

// Clamps a per-step delta into Mouse.move()'s signed-char range.
static int8_t clampStep(int16_t v) {
  if (v > MAX_MOVE) return MAX_MOVE;
  if (v < -MAX_MOVE) return -MAX_MOVE;
  return (int8_t)v;
}

// === MOVEMENT ===

static void moveToAbsolute(int16_t target_x, int16_t target_y) {
  int16_t dx = target_x - current_x;
  int16_t dy = target_y - current_y;

  while (dx != 0 || dy != 0) {
    int8_t step_x = clampStep(dx);
    int8_t step_y = clampStep(dy);

    Mouse.move(step_x, step_y, 0);

    dx -= step_x;
    dy -= step_y;
    current_x += step_x;
    current_y += step_y;

    delayMicroseconds(STEP_PACING_US);
  }

  current_x = target_x;
  current_y = target_y;
}

static void handleMoveAbsolute(char* params) {
  char* endptr;
  long xl = strtol(params, &endptr, 10);
  if (*endptr != ',') {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }
  long yl = strtol(endptr + 1, &endptr, 10);

  int16_t x = clampCoord(xl, 0, SCREEN_WIDTH - 1);
  int16_t y = clampCoord(yl, 0, SCREEN_HEIGHT - 1);

  moveToAbsolute(x, y);
  Serial.println(F("OK"));
}

static void handleMoveAbsoluteAndClick(char* params) {
  char* endptr;
  long xl = strtol(params, &endptr, 10);
  if (*endptr != ',') {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }
  long yl = strtol(endptr + 1, &endptr, 10);

  char button = 'L';
  if (*endptr == ',') {
    button = *(endptr + 1);
  }

  int16_t x = clampCoord(xl, 0, SCREEN_WIDTH - 1);
  int16_t y = clampCoord(yl, 0, SCREEN_HEIGHT - 1);

  moveToAbsolute(x, y);
  delay(DELAY_PRE_CLICK_SETTLE);

  switch (button) {
    case 'L':
      Mouse.press(MOUSE_LEFT);
      delay(DELAY_CLICK_HOLD);
      Mouse.release(MOUSE_LEFT);
      break;
    case 'R':
      Mouse.press(MOUSE_RIGHT);
      delay(DELAY_CLICK_HOLD);
      Mouse.release(MOUSE_RIGHT);
      break;
    case 'M':
      Mouse.press(MOUSE_MIDDLE);
      delay(DELAY_CLICK_HOLD);
      Mouse.release(MOUSE_MIDDLE);
      break;
  }

  Serial.println(F("OK"));
}

// === CLICK / HOLD / DOWN / UP ===

static void handleClick(char* params) {
  char button = params[0];

  switch (button) {
    case 'L':
      Mouse.press(MOUSE_LEFT);
      delay(DELAY_CLICK_HOLD);
      Mouse.release(MOUSE_LEFT);
      break;
    case 'R':
      Mouse.press(MOUSE_RIGHT);
      delay(DELAY_CLICK_HOLD);
      Mouse.release(MOUSE_RIGHT);
      break;
    case 'M':
      Mouse.press(MOUSE_MIDDLE);
      delay(DELAY_CLICK_HOLD);
      Mouse.release(MOUSE_MIDDLE);
      break;
    default:
      Serial.println(F("ERROR:INVALID_BUTTON"));
      return;
  }

  Serial.println(F("OK"));
}

static void handleHold(char* params) {
  char* comma = strchr(params, ',');
  if (comma == NULL) {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }

  char button = params[0];
  // NOTE: intentionally int (not long) - matches the original's `int holdMs =
  // params.substring(...).toInt()`, which already truncated to 16 bits here. Widening
  // this would change delay()'s behavior on a malformed/out-of-range hold value.
  int holdMs = (int)strtol(comma + 1, NULL, 10);

  switch (button) {
    case 'L': Mouse.press(MOUSE_LEFT); break;
    case 'R': Mouse.press(MOUSE_RIGHT); break;
    case 'M': Mouse.press(MOUSE_MIDDLE); break;
    default:
      Serial.println(F("ERROR:INVALID_BUTTON"));
      return;
  }

  delay(holdMs);

  switch (button) {
    case 'L': Mouse.release(MOUSE_LEFT); break;
    case 'R': Mouse.release(MOUSE_RIGHT); break;
    case 'M': Mouse.release(MOUSE_MIDDLE); break;
  }

  Serial.println(F("OK"));
}

static void handleMouseDown(char* params) {
  char button = params[0];

  switch (button) {
    case 'L': Mouse.press(MOUSE_LEFT); break;
    case 'R': Mouse.press(MOUSE_RIGHT); break;
    case 'M': Mouse.press(MOUSE_MIDDLE); break;
    default:
      Serial.println(F("ERROR:INVALID_BUTTON"));
      return;
  }

  Serial.println(F("OK"));
}

static void handleMouseUp(char* params) {
  char button = params[0];

  switch (button) {
    case 'L': Mouse.release(MOUSE_LEFT); break;
    case 'R': Mouse.release(MOUSE_RIGHT); break;
    case 'M': Mouse.release(MOUSE_MIDDLE); break;
    default:
      Serial.println(F("ERROR:INVALID_BUTTON"));
      return;
  }

  Serial.println(F("OK"));
}

// === SYNC (reset internal position tracking without moving the cursor) ===

static void handleSync(char* params) {
  char* endptr;
  long xl = strtol(params, &endptr, 10);
  if (*endptr != ',') {
    Serial.println(F("ERROR:INVALID_PARAMS"));
    return;
  }
  long yl = strtol(endptr + 1, NULL, 10);

  current_x = clampCoord(xl, 0, SCREEN_WIDTH - 1);
  current_y = clampCoord(yl, 0, SCREEN_HEIGHT - 1);

  Serial.println(F("OK"));
}

// === KEYBOARD ===

static void handleKeyboard(char* params) {
  if (strchr(params, '+') != NULL) {
    handleKeyboardShortcut(params);
    return;
  }

  if (params[0] == 'F') {
    long fKeyNum = strtol(params + 1, NULL, 10);
    if (fKeyNum >= 1 && fKeyNum <= 12) {
      int keyCode = KEY_F1 + (int)(fKeyNum - 1);
      Keyboard.press(keyCode);
      delay(DELAY_CLICK_HOLD);
      Keyboard.release(keyCode);
      Serial.println(F("OK"));
      return;
    }
    // fKeyNum out of range: fall through to the checks below, matching original behavior
  }

  if (strcmp(params, "ESC") == 0) {
    Keyboard.press(KEY_ESC);
    delay(DELAY_CLICK_HOLD);
    Keyboard.release(KEY_ESC);
    Serial.println(F("OK"));
    return;
  }

  if (strcmp(params, "ENTER") == 0) {
    Keyboard.press(KEY_RETURN);
    delay(DELAY_CLICK_HOLD);
    Keyboard.release(KEY_RETURN);
    Serial.println(F("OK"));
    return;
  }

  if (strcmp(params, "SPACE") == 0) {
    Keyboard.press(' ');
    delay(DELAY_CLICK_HOLD);
    Keyboard.release(' ');
    Serial.println(F("OK"));
    return;
  }

  if (strcmp(params, "TAB") == 0) {
    Keyboard.press(KEY_TAB);
    delay(DELAY_CLICK_HOLD);
    Keyboard.release(KEY_TAB);
    Serial.println(F("OK"));
    return;
  }

  if (strlen(params) == 1) {
    char c = params[0];
    Keyboard.press(c);
    delay(DELAY_CLICK_HOLD);
    Keyboard.release(c);
    Serial.println(F("OK"));
    return;
  }

  Serial.println(F("ERROR:INVALID_KEY"));
}

// Resolves one "+"-joined token to a keycode and presses or releases it.
static void pressToken(const char* key, bool press) {
  if (strcmp(key, "ALT") == 0) {
    press ? Keyboard.press(KEY_LEFT_ALT) : Keyboard.release(KEY_LEFT_ALT);
  } else if (strcmp(key, "CTRL") == 0) {
    press ? Keyboard.press(KEY_LEFT_CTRL) : Keyboard.release(KEY_LEFT_CTRL);
  } else if (strcmp(key, "SHIFT") == 0) {
    press ? Keyboard.press(KEY_LEFT_SHIFT) : Keyboard.release(KEY_LEFT_SHIFT);
  } else if (key[0] == 'F') {
    long fKeyNum = strtol(key + 1, NULL, 10);
    if (fKeyNum >= 1 && fKeyNum <= 12) {
      int keyCode = KEY_F1 + (int)(fKeyNum - 1);
      press ? Keyboard.press(keyCode) : Keyboard.release(keyCode);
    }
  } else if (strcmp(key, "ENTER") == 0) {
    press ? Keyboard.press(KEY_RETURN) : Keyboard.release(KEY_RETURN);
  } else if (strcmp(key, "ESC") == 0) {
    press ? Keyboard.press(KEY_ESC) : Keyboard.release(KEY_ESC);
  } else if (strcmp(key, "SPACE") == 0) {
    press ? Keyboard.press(' ') : Keyboard.release(' ');
  } else if (strcmp(key, "TAB") == 0) {
    press ? Keyboard.press(KEY_TAB) : Keyboard.release(KEY_TAB);
  } else if (strlen(key) == 1) {
    press ? Keyboard.press(key[0]) : Keyboard.release(key[0]);
  }
}

static void handleKeyboardShortcut(char* params) {
  constexpr uint8_t MAX_KEYS = 5;
  constexpr uint8_t KEY_TOKEN_SIZE = 12;  // fits "SHIFT"/"CTRL"/"F12" + null with margin
  char keyBuf[MAX_KEYS][KEY_TOKEN_SIZE];
  uint8_t keyCount = 0;

  uint8_t paramsLen = (uint8_t)strlen(params);
  uint8_t startPos = 0;

  while (startPos < paramsLen && keyCount < MAX_KEYS) {
    char* plusPtr = strchr(params + startPos, '+');
    uint8_t tokenLen;
    bool lastToken;

    if (plusPtr == NULL) {
      tokenLen = paramsLen - startPos;
      lastToken = true;
    } else {
      tokenLen = (uint8_t)(plusPtr - (params + startPos));
      lastToken = false;
    }

    uint8_t copyLen = tokenLen < (KEY_TOKEN_SIZE - 1) ? tokenLen : (KEY_TOKEN_SIZE - 1);
    memcpy(keyBuf[keyCount], params + startPos, copyLen);
    keyBuf[keyCount][copyLen] = '\0';
    keyCount++;

    if (lastToken) break;
    startPos += tokenLen + 1;  // skip past the '+'
  }

  for (uint8_t i = 0; i < keyCount; i++) {
    pressToken(keyBuf[i], true);
  }

  delay(DELAY_CLICK_HOLD);

  for (uint8_t i = keyCount; i > 0; i--) {
    pressToken(keyBuf[i - 1], false);
  }

  Serial.println(F("OK"));
}

// === ALT+CLICK (game-detection combo) ===

static void handleAltClick(char* params) {
  /*
   * CRITICAL TIMING SEQUENCE for game compatibility:
   *
   * Timeline:
   * +-------------------------------------------------------+
   * | Alt:   [10ms settle]===============[10ms cleanup]     |
   * | Mouse:              [====50ms hold====]                |
   * | Overlap:            [Alt+Mouse combo] <- Detected!     |
   * +-------------------------------------------------------+
   *
   * Total time: ~70ms (10 + 50 + 10)
   * Overlap time: 50ms (critical for game detection)
   */

  if (params[0] == '\0') {
    Serial.println(F("ERROR:MISSING_BUTTON"));
    return;
  }

  char button = params[0];

  // EARLY VALIDATION (fail-fast before pressing anything)
  uint8_t mouseButton;
  switch (button) {
    case 'L': mouseButton = MOUSE_LEFT; break;
    case 'R': mouseButton = MOUSE_RIGHT; break;
    case 'M': mouseButton = MOUSE_MIDDLE; break;
    default:
      Serial.println(F("ERROR:INVALID_BUTTON"));
      return;  // Clean exit - nothing pressed
  }

  // === EXECUTION (all inputs validated) ===

  // 1. Press Alt, let it settle
  Keyboard.press(KEY_LEFT_ALT);
  delay(DELAY_KEY_SETTLE);

  // 2. Press mouse button (Alt still held)
  Mouse.press(mouseButton);

  // 3. CRITICAL: Hold combo for game detection
  delay(DELAY_COMBO_HOLD);

  // 4. Release mouse first (prevents key ghosting)
  Mouse.release(mouseButton);

  // 5. Cleanup delay, then release Alt
  delay(DELAY_CLEANUP);
  Keyboard.release(KEY_LEFT_ALT);

  Serial.println(F("OK"));
}
