"""
SpiritVale minimap navigator.

Finds the 'SpiritVale' window, reads the minimap in its top-right corner, locates
red monster dots, and drives the left stick (via the Leonardo gamepad firmware in
arduino_joystick_leonardo_v1.ino) so the nearest dot moves toward the minimap
center -- the center is where the white player arrow sits, so a dot reaching the
center means the character has reached the monster.

Deps: pip install pyserial mss opencv-python numpy pywin32

Usage:
    python minimap_navigator.py --calibrate      # dump minimap crop + red mask to PNG
    python minimap_navigator.py --selftest       # no window/serial needed
    python minimap_navigator.py --dry-run        # detect + print stick, no serial
    python minimap_navigator.py --port COM5      # run for real
"""

import argparse
import math
import sys
import time

import cv2
import numpy as np

# === MINIMAP GEOMETRY (fractions of the game window's client area) ===
# ponytail: fractions, not pixels, so this survives a resolution change. These are a
# starting guess for a top-right minimap -- run --calibrate and tune until the saved
# crop is the minimap and nothing else.
MINIMAP_RIGHT_MARGIN = 0.015   # gap from window right edge
MINIMAP_TOP_MARGIN = 0.020     # gap from window top edge
MINIMAP_WIDTH = 0.150          # minimap size (square, as a fraction of window width)

# === RED DOT DETECTION (HSV) ===
# Red wraps around hue 0 in OpenCV's 0-179 hue space, so it needs two ranges.
RED_LO_1, RED_HI_1 = np.array([0, 120, 90]), np.array([10, 255, 255])
RED_LO_2, RED_HI_2 = np.array([170, 120, 90]), np.array([179, 255, 255])
MIN_DOT_AREA = 6          # px, rejects compression speckle
MAX_DOT_AREA = 400        # px, rejects large red UI elements that aren't monster dots

# === NAVIGATION ===
ARRIVED_RADIUS = 12       # px from center -> treat as reached, stop moving
DEADZONE_RADIUS = 4       # px, ignore jitter this close to center
STICK_MAX = 32767
STICK_MAGNITUDE = 1.0     # 0..1, how hard to push the stick (1.0 = full run)
MINIMAP_ROTATION_DEG = 0  # if the minimap is rotated relative to the stick, correct here
INVERT_STICK_Y = False    # flip if the character runs the wrong way vertically
LOOP_HZ = 20

WINDOW_EXE = "SpiritVale.exe"


# === WINDOW ===

def find_window_rect(exe=WINDOW_EXE):
    """
    Returns the client-area rect (left, top, width, height) in screen coords.

    Matches on the owning process's executable name, not the window title: an editor
    or explorer window with the project name in its title also matches the title, and
    EnumWindows order is not stable, so title matching picks the wrong window at random.
    """
    import win32api
    import win32con
    import win32gui
    import win32process

    matches = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        try:
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
            path = win32process.GetModuleFileNameEx(handle, 0)
        except Exception:
            return  # system/protected process we can't query - not the game
        if path.rsplit("\\", 1)[-1].lower() == exe.lower():
            matches.append(hwnd)

    win32gui.EnumWindows(enum_cb, None)
    if not matches:
        raise RuntimeError(f"no visible window owned by {exe!r} - is the game running?")

    hwnd = matches[0]
    # Client rect excludes the title bar and borders, so the fractions above are
    # measured against the actual rendered game image.
    l, t, r, b = win32gui.GetClientRect(hwnd)
    sl, st = win32gui.ClientToScreen(hwnd, (l, t))
    return sl, st, r - l, b - t


def minimap_region(win_rect):
    wl, wt, ww, wh = win_rect
    size = int(ww * MINIMAP_WIDTH)  # square minimap; both dims scale off window width
    left = wl + ww - int(ww * MINIMAP_RIGHT_MARGIN) - size
    top = wt + int(ww * MINIMAP_TOP_MARGIN)
    return {"left": left, "top": top, "width": size, "height": size}


# === DETECTION ===

def red_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, RED_LO_1, RED_HI_1) | cv2.inRange(hsv, RED_LO_2, RED_HI_2)
    # Single open pass kills isolated speckle without eating the small dots.
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def find_dots(mask):
    """Returns [(cx, cy, area)] of red blobs, in mask pixel coords."""
    n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):  # 0 is background
        area = stats[i, cv2.CC_STAT_AREA]
        if MIN_DOT_AREA <= area <= MAX_DOT_AREA:
            cx, cy = centroids[i]
            out.append((float(cx), float(cy), int(area)))
    return out


def nearest_dot(dots, center):
    """Nearest dot to center, or None. Returns (cx, cy, area, dist)."""
    if not dots:
        return None
    cx0, cy0 = center
    best = min(dots, key=lambda d: (d[0] - cx0) ** 2 + (d[1] - cy0) ** 2)
    dist = math.hypot(best[0] - cx0, best[1] - cy0)
    return (best[0], best[1], best[2], dist)


# === NAVIGATION MATH ===

def stick_for(dx, dy):
    """
    Map a minimap offset (dot minus center, screen axes: +x right, +y DOWN) to a
    left-stick vector in the firmware's -32767..32767 range.

    Screen +y is down but stick +y is up, so y is negated once here. INVERT_STICK_Y
    flips it again for games that read the stick the other way.
    """
    dist = math.hypot(dx, dy)
    if dist <= DEADZONE_RADIUS:
        return 0, 0

    ux, uy = dx / dist, dy / dist

    if MINIMAP_ROTATION_DEG:
        a = math.radians(MINIMAP_ROTATION_DEG)
        ux, uy = ux * math.cos(a) - uy * math.sin(a), ux * math.sin(a) + uy * math.cos(a)

    sx = ux * STICK_MAGNITUDE
    sy = -uy * STICK_MAGNITUDE
    if INVERT_STICK_Y:
        sy = -sy

    clamp = lambda v: max(-STICK_MAX, min(STICK_MAX, int(round(v * STICK_MAX))))
    return clamp(sx), clamp(sy)


# === GAMEPAD ===

class Pad:
    """Thin wrapper over the serial protocol. Every command blocks on its reply."""

    def __init__(self, port, baud=115200):
        import serial
        self.ser = serial.Serial(port, baud, timeout=1)
        time.sleep(2)  # 32u4 resets when the port opens
        line = self.ser.readline().strip()
        if line != b"READY":
            raise RuntimeError(f"firmware did not report READY (got {line!r})")

    def cmd(self, s):
        self.ser.write(f"{s}\n".encode())
        return self.ser.readline().strip()

    def left_stick(self, x, y):
        return self.cmd(f"L{x},{y}")

    def neutral(self):
        return self.cmd("Z")

    def button_down(self, n):
        return self.cmd(f"D{n}")

    def button_up(self, n):
        return self.cmd(f"U{n}")

    def close(self):
        try:
            self.neutral()
        finally:
            self.ser.close()


class DryPad:
    def left_stick(self, x, y):
        print(f"  stick -> {x:6d},{y:6d}")

    def neutral(self):
        print("  stick -> neutral")

    def close(self):
        pass


# === MODES ===

def grab(sct, region):
    frame = np.asarray(sct.grab(region))
    return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


def calibrate():
    import mss
    rect = find_window_rect()
    region = minimap_region(rect)
    print(f"window client rect: {rect}")
    print(f"minimap region:     {region}")

    with mss.mss() as sct:
        bgr = grab(sct, region)

    mask = red_mask(bgr)
    dots = find_dots(mask)
    center = (bgr.shape[1] / 2, bgr.shape[0] / 2)

    overlay = bgr.copy()
    cv2.drawMarker(overlay, (int(center[0]), int(center[1])), (255, 255, 0),
                   cv2.MARKER_CROSS, 20, 1)
    for cx, cy, area in dots:
        cv2.circle(overlay, (int(cx), int(cy)), 8, (0, 255, 0), 1)

    cv2.imwrite("calib_minimap.png", bgr)
    cv2.imwrite("calib_mask.png", mask)
    cv2.imwrite("calib_overlay.png", overlay)
    print(f"found {len(dots)} red dot(s): {[(round(d[0]), round(d[1]), d[2]) for d in dots]}")
    print("wrote calib_minimap.png / calib_mask.png / calib_overlay.png")
    print("Tune MINIMAP_* fractions until calib_minimap.png is exactly the minimap.")


def run(pad, show=False):
    import mss

    rect = find_window_rect()
    region = minimap_region(rect)
    print(f"tracking minimap at {region}, Ctrl-C to stop")

    period = 1.0 / LOOP_HZ
    moving = False

    with mss.mss() as sct:
        while True:
            t0 = time.time()
            bgr = grab(sct, region)
            center = (bgr.shape[1] / 2, bgr.shape[0] / 2)
            target = nearest_dot(find_dots(red_mask(bgr)), center)

            if target is None:
                # No monster visible. Also covers the case where the only dot is
                # under the player arrow and fully concealed by it.
                if moving:
                    pad.neutral()
                    moving = False
                print("no target")
            else:
                cx, cy, area, dist = target
                if dist <= ARRIVED_RADIUS:
                    if moving:
                        pad.neutral()
                        moving = False
                    print(f"arrived (dist {dist:.1f}px)")
                else:
                    sx, sy = stick_for(cx - center[0], cy - center[1])
                    pad.left_stick(sx, sy)
                    moving = True
                    print(f"target dist {dist:.1f}px -> stick {sx},{sy}")

            if show:
                cv2.imshow("minimap", bgr)
                if cv2.waitKey(1) & 0xFF == 27:
                    break

            sleep = period - (time.time() - t0)
            if sleep > 0:
                time.sleep(sleep)


# === SELF-CHECK ===

def selftest():
    """Synthetic minimap: detection, nearest-pick, and stick direction."""
    img = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(img, (150, 100), 4, (0, 0, 255), -1)   # far, due east
    cv2.circle(img, (100, 60), 4, (0, 0, 255), -1)    # near, due north
    cv2.circle(img, (30, 30), 4, (255, 255, 255), -1)  # white arrow, must be ignored

    dots = find_dots(red_mask(img))
    assert len(dots) == 2, f"expected 2 red dots, got {len(dots)}"

    center = (100.0, 100.0)
    cx, cy, _, dist = nearest_dot(dots, center)
    assert abs(cx - 100) < 3 and abs(cy - 60) < 3, f"picked wrong dot: {cx},{cy}"
    assert abs(dist - 40) < 3, dist

    # Dot is above center -> push stick up (+y), no sideways component.
    sx, sy = stick_for(cx - center[0], cy - center[1])
    assert abs(sx) < 2000 and sy > 30000, f"north -> {sx},{sy}"

    # Dot to the right -> push stick right.
    sx, sy = stick_for(50, 0)
    assert sx > 30000 and abs(sy) < 2000, f"east -> {sx},{sy}"

    # Inside the deadzone -> no movement.
    assert stick_for(1, 1) == (0, 0)

    assert nearest_dot([], center) is None
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port of the Leonardo, e.g. COM5")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="detect and print, no serial")
    ap.add_argument("--show", action="store_true", help="live minimap preview window")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.calibrate:
        calibrate()
        return

    if args.dry_run:
        pad = DryPad()
    elif args.port:
        pad = Pad(args.port)
    else:
        sys.exit("need --port COMx (or --dry-run / --calibrate / --selftest)")

    try:
        run(pad, show=args.show)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        pad.close()


if __name__ == "__main__":
    main()
