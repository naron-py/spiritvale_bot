"""SpiritVale minimap bot: chase nearest red dot with left stick.

deps: pip install mss opencv-python numpy pygetwindow
  virtual pad:  pip install vgamepad     (needs the ViGEmBus driver, one-time)
  real HID pad: pip install pyserial     (Leonardo running arduino_joystick_leonardo_v1.ino)

usage:
  python minimap_bot.py               # vgamepad
  python minimap_bot.py --port COM5   # Arduino Leonardo
  python minimap_bot.py --demo        # offline self-check
"""
import sys
import time

import cv2
import numpy as np

WINDOW_TITLE = "SpiritVale"
# Square capture box centred on the player arrow. cx/cy are fractions of the
# client area, r is a fraction of client width (the chase radius).
# ponytail: this is the calibration knob -- run --snap and nudge until the cyan
# cross lands on the white arrow.
MINIMAP = dict(cx=0.927, cy=0.152, r=0.055)
SPEED = 1.0              # stick magnitude while chasing, 0..1
DEADZONE_PX = 4          # blob this close to center = arrived
CONCEAL_PX = 20          # white player arrow hides a dot inside this radius
LOST_HOLD_S = 1.0        # keep last heading this long after a dot vanishes
ATTACK_MASH = False      # False = hold L1 down; True = tap it on the cycle below
ATTACK_PERIOD_S = 0.40   # mash cycle, ignored while ATTACK_MASH is False
ATTACK_HOLD_S = 0.15     # how long L1 stays down each mash cycle
BUFF_PERIOD_S = 60.0     # recast the buff sequence this often
BUFF_SEQUENCE = ("up", "left", "down", "right")
SPAM_BUTTON = "y"        # tapped on a timer all the while the bot runs; None = off
SPAM_PERIOD_S = 0.5
SPAM_HOLD_S = 0.05
WAKE_AMP = 0.5           # stick nudge that flips the game into controller mode
WAKE_STEP_S = 0.15
WAKE_SETTLE_S = 0.5      # grace after the nudge before the first button press
BUFF_HOLD_S = 0.25       # each d-pad press
BUFF_GAP_S = 0.80        # pause between presses -- must outlast the cast animation
MIN_BLOB_AREA = 6        # px, filters compression speckle
# Red mushroom caps painted on the terrain are what the bot kept walking to.
# Size cannot separate them -- an occluded cap is a sliver the size of a dot, and
# merged dots are the size of a cap. Colour can: monster dots are drawn pure red
# (H 0, S 255), every mushroom pixel is desaturated pink (S 94-154). Anything
# under this floor is terrain art. Sample a stray blob's HSV before touching it.
RED_S_MIN = 200
PLAYER_AREA = (25, 600)  # px area range for the white player arrow
LOOP_HZ = 20


def wake_controller(pad):
    """SpiritVale stays in keyboard mode until it sees stick motion, and button
    presses sent before that are dropped. A there-and-back nudge flips it."""
    for sx, sy in ((0.0, WAKE_AMP), (0.0, -WAKE_AMP), (0.0, 0.0)):
        pad.stick(sx, sy, False)
        time.sleep(WAKE_STEP_S)
    # The game eats the first button press while it swaps input modes, which is
    # why the leading d-pad press went missing. Let the swap finish.
    time.sleep(WAKE_SETTLE_S)


def end_key_hit():
    """True once per physical End press, works while the game has focus.

    GetAsyncKeyState's low bit means 'pressed since the last call', so polling it
    is already edge-detected -- no key hook, no extra dependency.
    """
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x23) & 1)


def find_window():
    import pygetwindow as gw
    wins = [w for w in gw.getWindowsWithTitle(WINDOW_TITLE) if w.title.strip() == WINDOW_TITLE]
    if not wins:
        wins = gw.getWindowsWithTitle(WINDOW_TITLE)
    if not wins:
        raise RuntimeError(f"no window titled {WINDOW_TITLE!r}")
    return wins[0]


def minimap_region(win):
    r = int(win.width * MINIMAP["r"])
    x = win.left + int(win.width * MINIMAP["cx"])
    y = win.top + int(win.height * MINIMAP["cy"])
    return dict(left=x - r, top=y - r, width=2 * r, height=2 * r)


def find_red_dots(bgr):
    """Return [(x, y, width)] of monster dots, image coords. width = short side."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # red wraps hue 0, so two bands
    mask = cv2.inRange(hsv, (0, RED_S_MIN, 90), (10, 255, 255)) | \
           cv2.inRange(hsv, (170, RED_S_MIN, 90), (180, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        if cv2.contourArea(c) < MIN_BLOB_AREA:
            continue
        # reported for --snap only: dots merge when they cluster, so width is a
        # diagnostic, not a filter -- rejecting fat blobs loses packed monsters.
        width = min(cv2.minAreaRect(c)[1])
        m = cv2.moments(c)
        if m["m00"] == 0:
            continue
        out.append((m["m10"] / m["m00"], m["m01"] / m["m00"], width))
    return out


def find_player(bgr):
    """Centroid of the white player arrow, or None. Nearest white blob to centre."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 200), (180, 45, 255))  # white: bright, unsaturated
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = bgr.shape[:2]
    best, best_d = None, 1e9
    for c in cnts:
        if not (PLAYER_AREA[0] <= cv2.contourArea(c) <= PLAYER_AREA[1]):
            continue
        m = cv2.moments(c)
        if m["m00"] == 0:
            continue
        x, y = m["m10"] / m["m00"], m["m01"] / m["m00"]
        d = (x - w / 2) ** 2 + (y - h / 2) ** 2
        if d < best_d:
            best, best_d = (x, y), d
    return best


def nearest(dots, cx, cy):
    return min(dots, key=lambda d: (d[0] - cx) ** 2 + (d[1] - cy) ** 2, default=None)


def stick_vector(dx, dy, radius):
    """Screen delta -> left-stick (x, y), y up positive.

    Direction only, magnitude SPEED. A minimap pixel is many world metres, so
    scaling tilt by pixel distance made every move a crawl the game's own
    deadzone swallowed. ponytail: drop SPEED below 1.0 only if you overshoot.
    """
    n = max((dx * dx + dy * dy) ** 0.5, 1e-6)
    scale = SPEED / n
    return float(np.clip(dx * scale, -1, 1)), float(np.clip(-dy * scale, -1, 1))


class VirtualPad:
    """vgamepad backend."""

    def __init__(self):
        import vgamepad as vg
        self.pad = vg.VX360Gamepad()
        self.attack_btn = vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER  # L1 / LB
        self.dpad = {"up": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
                     "down": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
                     "left": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
                     "right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT}
        self.face = {"a": vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
                     "b": vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                     "x": vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
                     "y": vg.XUSB_BUTTON.XUSB_GAMEPAD_Y}

    def _tap(self, btn, hold):
        # The stick keeps its last value across this: left_joystick_float persists
        # between updates, so a tap never interrupts the chase.
        self.pad.press_button(btn)
        self.pad.update()
        time.sleep(hold)
        self.pad.release_button(btn)
        self.pad.update()

    def tap_dpad(self, name, hold):
        self._tap(self.dpad[name], hold)

    def tap_button(self, name, hold=SPAM_HOLD_S):
        self._tap(self.face[name], hold)

    def stick(self, sx, sy, attack=False):
        self.pad.left_joystick_float(sx, sy)
        if attack:
            self.pad.press_button(self.attack_btn)
        else:
            self.pad.release_button(self.attack_btn)
        self.pad.update()  # one report per frame

    def close(self):
        self.stick(0.0, 0.0, False)


class ArduinoPad:
    """arduino_joystick_leonardo_v1.ino over serial. 'L<x>,<y>' -> 'OK'."""

    # ATmega32U4 boards: Arduino LLC, Arduino SA, SparkFun Pro Micro.
    VIDS = (0x2341, 0x2A03, 0x1B4F)

    @staticmethod
    def autodetect():
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        for p in ports:
            if p.vid in ArduinoPad.VIDS:
                return p.device
        listing = "\n  ".join(f"{p.device}  {p.description}" for p in ports) or "(none)"
        raise RuntimeError(f"no Arduino found. ports present:\n  {listing}")

    def __init__(self, port=None, baud=115200):
        import serial
        if port in (None, "auto"):
            port = self.autodetect()
            print(f"auto-detected {port}")
        try:
            self.ser = serial.Serial(port, baud, timeout=0.5)
        except serial.SerialException as e:
            from serial.tools import list_ports
            have = ", ".join(p.device for p in list_ports.comports()) or "(none)"
            raise RuntimeError(f"{port} not usable ({e}). ports present: {have}") from None
        # Opening at 115200 does not reset a Leonardo, so waiting for its READY
        # banner just burns the whole timeout on an already-booted board. Ping
        # instead, and keep pinging: a board that IS mid-reset drops the first one.
        deadline = time.time() + 6
        while time.time() < deadline:
            self.ser.write(b"P\n")
            if self.ser.readline().strip() in (b"PONG", b"READY"):
                break
        else:
            raise RuntimeError(f"{port} opened but no PONG -- "
                               f"wrong port, or sketch not flashed")
        self.ser.reset_input_buffer()  # drop READY/PONG backlog before commands
        self.ser.timeout = 2
        self.last = None

    ATTACK_BTN = 4  # LB in the usual XInput button order
    HAT = {"up": 0, "right": 2, "down": 4, "left": 6}  # sketch: 0..7 clockwise from N
    FACE = {"a": 0, "b": 1, "x": 2, "y": 3}  # usual XInput button order

    def tap_dpad(self, name, hold):
        self._cmd(f"V{self.HAT[name]}")
        time.sleep(hold)
        self._cmd("V-1")  # -1 centres the hat

    def tap_button(self, n, hold=None):
        # hold is ignored: the sketch's own B command is press, 50ms, release.
        self._cmd(f"B{self.FACE.get(n, n)}")

    def _cmd(self, line):
        self.ser.write(f"{line}\n".encode())
        reply = self.ser.readline().strip()
        if reply != b"OK":
            print(f"\nboard replied {reply!r} to {line}")

    def stick(self, sx, sy, attack=False):
        # HID Y axis is down-positive, our sy is up-positive.
        x, y = int(sx * 32767), int(-sy * 32767)
        if (x, y, attack) == self.last:
            return  # ponytail: sketch is synchronous, skip no-op round trips
        if self.last is None or (x, y) != self.last[:2]:
            self._cmd(f"L{x},{y}")
        if self.last is None or attack != self.last[2]:
            self._cmd(f"{'D' if attack else 'U'}{self.ATTACK_BTN}")
        self.last = (x, y, attack)

    def close(self):
        self.ser.write(b"Z\n")
        self.ser.readline()
        self.ser.close()


def main(port=None):
    import mss

    win = find_window()
    pad = ArduinoPad(port) if port else VirtualPad()
    print(f"window {win.width}x{win.height} @ ({win.left},{win.top})"
          f" via {type(pad).__name__} -- End to pause, ctrl+c to stop")

    last = None  # (t, dist, sx, sy) of last seen dot
    paused = False
    next_buff = 0.0   # 0 = cast once at startup, then every BUFF_PERIOD_S
    buff_queue = []   # d-pad presses left in the current cast
    next_press = 0.0  # earliest time for the next one
    next_spam = 0.0   # SPAM_BUTTON goes out on its own timer

    # Once, up front: the game ignores buttons until it has seen stick motion.
    # After this the chase keeps it awake, so the buff never has to stop to nudge.
    wake_controller(pad)

    with mss.mss() as sct:
        try:
            while True:
                if end_key_hit():
                    paused = not paused
                    if paused:
                        pad.stick(0.0, 0.0, False)  # drop stick and L1 while parked
                        last = None
                    print(f"\n{'PAUSED' if paused else 'RESUMED'} (End)")
                if paused:
                    time.sleep(0.05)
                    continue

                if not buff_queue and time.time() >= next_buff:
                    buff_queue = list(BUFF_SEQUENCE)
                    next_buff = time.time() + BUFF_PERIOD_S
                    print(f"\nbuffing: {' '.join(BUFF_SEQUENCE)}")

                reg = minimap_region(win)
                img = np.array(sct.grab(reg))[:, :, :3]
                h, w = img.shape[:2]
                # Arrow position beats the box centre: the box drifts with UI scale,
                # the arrow is where the character actually is.
                cx, cy = find_player(img) or (w / 2, h / 2)

                # Blobs under the player arrow are never a target: either we already
                # arrived, or it is a fixed red UI element sitting at the centre.
                dots = [d for d in find_red_dots(img)
                        if (d[0] - cx) ** 2 + (d[1] - cy) ** 2 > CONCEAL_PX ** 2]
                dot = nearest(dots, cx, cy)
                now = time.time()

                if dot is not None:
                    dx, dy = dot[0] - cx, dot[1] - cy
                    dist = (dx * dx + dy * dy) ** 0.5
                    sx, sy = stick_vector(dx, dy, min(w, h) / 2)
                    last = (now, dist, sx, sy)
                    if dist < DEADZONE_PX:
                        sx = sy = 0.0
                        state = "centered"
                    else:
                        state = f"dist {dist:6.1f}"
                elif last and last[1] < CONCEAL_PX and now - last[0] < LOST_HOLD_S:
                    # dot vanished right under the white player arrow -> we are on it
                    sx = sy = 0.0
                    state = "concealed"
                elif last and now - last[0] < LOST_HOLD_S:
                    # brief flicker/occlusion mid-chase: coast on last heading
                    _, _, sx, sy = last
                    state = "coasting"
                else:
                    sx = sy = 0.0
                    last = None
                    state = "no monster"

                # L1 held down continuously. Set ATTACK_MASH to mash it instead.
                atk = (now % ATTACK_PERIOD_S) < ATTACK_HOLD_S if ATTACK_MASH else True
                pad.stick(sx, sy, atk)

                # One d-pad press per pass, spaced by BUFF_GAP_S. The stick and L1
                # keep their last value across a tap, so the buff casts mid-chase
                # instead of parking the bot for a whole sequence.
                key = ""
                if buff_queue and now >= next_press:
                    key = buff_queue.pop(0)
                    pad.tap_dpad(key, BUFF_HOLD_S)
                    next_press = now + BUFF_GAP_S
                elif SPAM_BUTTON and now >= next_spam:
                    # Never in the same pass as a buff press: two taps back to back
                    # land inside one another's animation and the game drops one.
                    pad.tap_button(SPAM_BUTTON, SPAM_HOLD_S)
                    key = SPAM_BUTTON
                    next_spam = now + SPAM_PERIOD_S

                print(f"{state:12} stick {sx:+.2f},{sy:+.2f} atk {'#' if atk else '.'}"
                      f" {key:5}  ", end="\r")
                time.sleep(1 / LOOP_HZ)
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            pad.close()


def demo():
    """Self-check: synthetic minimap, no game or gamepad needed."""
    img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(img, (150, 60), 4, (0, 0, 255), -1)   # far, up-right
    cv2.circle(img, (120, 100), 4, (0, 0, 255), -1)  # near, right
    cv2.circle(img, (40, 40), 4, (255, 0, 0), -1)    # blue, must be ignored
    # Mushroom art, measured off a real minimap: desaturated pink, and an occluded
    # cap is a dot-sized sliver -- only the colour tells these from a monster.
    cv2.circle(img, (60, 150), 20, (82, 93, 176), -1)   # whole cap
    cv2.circle(img, (30, 60), 4, (82, 93, 176), -1)     # sliver of one
    cv2.circle(img, (150, 170), 4, (0, 0, 255), -1)  # two monsters touching -> one
    cv2.circle(img, (157, 170), 4, (0, 0, 255), -1)  # fat contour, must still count

    cv2.circle(img, (105, 95), 6, (255, 255, 255), -1)  # player arrow, near centre
    cv2.circle(img, (10, 190), 9, (255, 255, 255), -1)  # other white UI, farther off

    dots = find_red_dots(img)
    assert len(dots) == 3, dots       # 2 singles + the merged pair, no mushrooms
    px, py = find_player(img)
    assert abs(px - 105) < 3 and abs(py - 95) < 3, (px, py)
    x, y, _ = nearest(dots, 100, 100)
    assert abs(x - 120) < 3 and abs(y - 100) < 3, (x, y)

    sx, sy = stick_vector(x - 100, y - 100, 100)
    assert sx > 0.95 and abs(sy) < 0.05, (sx, sy)      # push right, full tilt
    sx, sy = stick_vector(0, -50, 100)
    assert sy > 0.95 and abs(sx) < 0.05, (sx, sy)      # up = +y
    sx, sy = stick_vector(3, -4, 100)                  # near target, still full
    assert abs((sx * sx + sy * sy) ** 0.5 - 1.0) < 0.01, (sx, sy)

    # ArduinoPad wire format, no board attached
    pad = ArduinoPad.__new__(ArduinoPad)
    sent = []
    pad.ser = type("S", (), {"write": lambda _, b: sent.append(b),
                             "readline": lambda _: b"OK"})()
    pad.last = None
    pad.stick(0.0, 1.0)             # stick up -> HID Y negative
    pad.stick(0.0, 1.0)             # repeat must not re-send
    pad.stick(-1.0, 0.0, True)      # move + attack down
    pad.stick(-1.0, 0.0, False)     # stick unchanged -> only the release
    assert sent == [b"L0,-32767\n", b"U4\n",
                    b"L-32767,0\n", b"D4\n",
                    b"U4\n"], sent

    sent.clear()
    for key in BUFF_SEQUENCE:
        pad.tap_dpad(key, 0)
    assert sent == [b"V0\n", b"V-1\n", b"V6\n", b"V-1\n",
                    b"V4\n", b"V-1\n", b"V2\n", b"V-1\n"], sent

    sent.clear()
    pad.tap_button(SPAM_BUTTON)   # by name
    pad.tap_button(3)             # same button by index
    assert sent == [b"B3\n", b"B3\n"], sent

    print("demo ok")


def snap(path="minimap_snap.png"):
    """Dump what the bot sees: captured region, detections circled, centre marked."""
    import mss
    win = find_window()
    with mss.mss() as sct:
        img = np.array(sct.grab(minimap_region(win)))[:, :, :3].copy()
    h, w = img.shape[:2]
    player = find_player(img)
    print(f"  player arrow: {player or 'NOT FOUND -- falling back to box centre'}")
    cx, cy = (int(player[0]), int(player[1])) if player else (w // 2, h // 2)
    for x, y, wpx in find_red_dots(img):
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        hid = d <= CONCEAL_PX
        cv2.circle(img, (int(x), int(y)), 8, (0, 0, 255) if hid else (0, 255, 0), 1)
        print(f"  dot at ({x:6.1f},{y:6.1f}) width {wpx:5.1f} dist {d:6.1f}"
              f"{'  REJECTED: under player arrow' if hid else ''}")
    cv2.drawMarker(img, (w // 2, h // 2), (255, 0, 255), cv2.MARKER_CROSS, 10, 1)
    cv2.drawMarker(img, (cx, cy), (255, 255, 0), cv2.MARKER_CROSS, 14, 1)
    cv2.circle(img, (cx, cy), CONCEAL_PX, (255, 255, 0), 1)
    cv2.imwrite(path, img)
    print(f"{w}x{h} region -> {path}")


def probe(hold=0.4, gap=2.0):
    """Press every X360 button in turn, naming each. Watch which one buffs."""
    import vgamepad as vg
    pad = vg.VX360Gamepad()
    names = [n for n in dir(vg.XUSB_BUTTON) if n.startswith("XUSB_GAMEPAD_")]
    print(f"focus the game NOW -- 3s, then {len(names)} buttons, {gap}s apart")
    time.sleep(3)
    try:
        for sx, sy in ((0.0, WAKE_AMP), (0.0, -WAKE_AMP), (0.0, 0.0)):
            pad.left_joystick_float(sx, sy)
            pad.update()
            time.sleep(WAKE_STEP_S)
        time.sleep(WAKE_SETTLE_S)
        for n in names:
            b = getattr(vg.XUSB_BUTTON, n)
            print(f"  {n.replace('XUSB_GAMEPAD_', '')}")
            pad.press_button(b)
            pad.update()
            time.sleep(hold)
            pad.release_button(b)
            pad.update()
            time.sleep(gap)
        # Triggers are axes, not buttons -- probe them too.
        for label, setter in (("LEFT_TRIGGER", pad.left_trigger_float),
                              ("RIGHT_TRIGGER", pad.right_trigger_float)):
            print(f"  {label}")
            setter(1.0)
            pad.update()
            time.sleep(hold)
            setter(0.0)
            pad.update()
            time.sleep(gap)
    finally:
        pad.reset()
        pad.update()
    print("done -- tell me which name was on screen when a buff cast")


def buff_test(port=None, hold=BUFF_HOLD_S, gap=BUFF_GAP_S):
    """Fire the buff sequence once so timing can be tuned without a 60s wait."""
    pad = ArduinoPad(port) if port else VirtualPad()
    print(f"focus the game NOW -- 3s, then {' '.join(BUFF_SEQUENCE)} "
          f"(hold {hold}s, gap {gap}s)")
    time.sleep(3)
    try:
        wake_controller(pad)
        for key in BUFF_SEQUENCE:
            print(f"  {key}")
            pad.tap_dpad(key, hold)
            time.sleep(gap)
    finally:
        pad.close()
    print("done -- if nothing cast, raise hold/gap")


TAP_HELP = ("button 0-15 or a/b/x/y, d-pad u/d/l/r, stick lx/ly/rx/ry "
            "(prefix - for the other direction, e.g. -lx = left)")
_DIRS = {"u": "up", "d": "down", "l": "left", "r": "right"}


def tap_one(pad, token):
    """Fire one control named the way TAP_HELP describes it. True if understood."""
    token = token.strip().lower()
    sign = -1 if token.startswith("-") else 1
    token = token.lstrip("-+")
    if token in _DIRS:
        pad.tap_dpad(_DIRS[token], 0.2)
    elif token in ("lx", "ly", "rx", "ry"):
        # Full deflection and back. The wizard asks for one direction at a time,
        # so each axis needs both signs: -lx is left, lx is right.
        axis = "L" if token[0] == "l" else "R"
        v = 32767 * sign
        x, y = (v, 0) if token[1] == "x" else (0, v)
        pad._cmd(f"{axis}{x},{y}")
        time.sleep(0.4)
        pad._cmd(f"{axis}0,0")
    elif token in ArduinoPad.FACE:
        pad.tap_button(token)
    elif token.isdigit():
        pad.tap_button(int(token))
    else:
        return False
    return True


def press_repl(port="auto"):
    """Tap one control at a time, on demand.

    Steam has no mapping for an unknown HID pad, so it runs a setup wizard that
    asks you to press each control in turn -- and nothing reaches the game until
    that is done. Nobody can press a button on a board with no buttons, hence
    this: type what the wizard is waiting for. One connection stays open for the
    whole run, so the board never resets mid-wizard.
    """
    pad = ArduinoPad(port)
    print(f"{TAP_HELP}\nblank line quits")
    try:
        while True:
            s = input("> ").strip()
            if not s:
                break
            if not tap_one(pad, s):
                print(f"  ? expected {TAP_HELP}")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        pad.close()
        print("\nclosed")


def stick_test(port=None, seconds=12):
    """Blind pad check: walk a circle. Character moves -> pad fine, vision is the bug."""
    pad = ArduinoPad(port) if port else VirtualPad()
    print("focus the game NOW; walking a circle for", seconds, "s")
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            a = (time.time() - t0) * 1.2
            sx, sy = np.cos(a), np.sin(a)
            pad.stick(float(sx), float(sy), False)
            print(f"stick {sx:+.2f},{sy:+.2f}   ", end="\r")
            time.sleep(1 / LOOP_HZ)
    except KeyboardInterrupt:
        pass  # cutting the test short is normal, not a crash
    finally:
        pad.close()
        print("\ndone")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--snap" in sys.argv:
        snap()
    elif "--probe" in sys.argv:
        probe()
    elif "--press" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        press_repl(sys.argv[i + 1] if i >= 0 else "auto")
    elif "--buff" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        j = sys.argv.index("--buff")
        rest = [a for a in sys.argv[j + 1:] if not a.startswith("--")]
        hold = float(rest[0]) if len(rest) > 0 else BUFF_HOLD_S
        gap = float(rest[1]) if len(rest) > 1 else BUFF_GAP_S
        buff_test(sys.argv[i + 1] if i >= 0 else None, hold, gap)
    elif "--test" in sys.argv:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        stick_test(sys.argv[i + 1] if i >= 0 else None)
    else:
        i = sys.argv.index("--port") if "--port" in sys.argv else -1
        main(sys.argv[i + 1] if i >= 0 else None)
