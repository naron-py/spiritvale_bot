"""External, read-only editor for overlapping SpiritVale farming circles.

The panel is a top-down world-coordinate preview, not a 3D terrain projection.
It reads only the local player's position and never sends game input.
"""

import ctypes
import math
import os
import struct
import sys
import time
import tkinter as tk

from minimap_bot import AREA_FILE, Area, find_window

F2 = 0x71
F3 = 0x72
F4 = 0x73
BACKSPACE = 0x08
END = 0x23
ESCAPE = 0x1B
POLL_MS = 50
PANEL_SIZE = 500
PANEL_PAD = 36
RADIUS_STEP = 5.0
MIN_RADIUS = 5.0


class Hotkeys:
    """Global edge detector that does not depend on which window has focus."""

    def __init__(self, get_state=None):
        self.get_state = get_state or ctypes.windll.user32.GetAsyncKeyState
        self.down = {}

    def hit(self, key):
        down = bool(self.get_state(key) & 0x8000)
        hit = down and not self.down.get(key, False)
        self.down[key] = down
        return hit


def view_transform(circles, player, width, height, preview_radius=0.0,
                   pad=PANEL_PAD):
    """Return world-to-panel scale and origin, fitting circles plus the player."""
    px, pz = player
    extents = [(x - r, z - r, x + r, z + r) for x, z, r in circles]
    extents.append((px - preview_radius, pz - preview_radius,
                    px + preview_radius, pz + preview_radius))
    x0 = min(e[0] for e in extents)
    z0 = min(e[1] for e in extents)
    x1 = max(e[2] for e in extents)
    z1 = max(e[3] for e in extents)
    span_x, span_z = max(x1 - x0, 1.0), max(z1 - z0, 1.0)
    scale = min(max(width - pad * 2, 1) / span_x,
                max(height - pad * 2, 1) / span_z)
    world_cx, world_cz = (x0 + x1) / 2, (z0 + z1) / 2
    origin_x = width / 2 - world_cx * scale
    origin_y = height / 2 + world_cz * scale
    return scale, origin_x, origin_y


def project_circle(circle, transform):
    """Map one world circle to a Tk canvas bounding box."""
    x, z, radius = circle
    scale, ox, oy = transform
    sx, sy, sr = ox + x * scale, oy - z * scale, radius * scale
    return sx - sr, sy - sr, sx + sr, sy + sr


def _click_through(root):
    """Keep the overlay visible without stealing mouse focus from the game."""
    try:
        user32 = ctypes.windll.user32
        client = ctypes.c_void_p(root.winfo_id())
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        # Tk's winfo_id() is the drawable client HWND. Layering that child lets
        # its static border through but suppresses later canvas items (including
        # every F2 circle). Extended window styles belong on Tk's native wrapper.
        hwnd = user32.GetParent(client) or client
        get_long = user32.GetWindowLongW
        set_long = user32.SetWindowLongW
        get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        exstyle = get_long(hwnd, -20)
        # WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        set_long(hwnd, -20, exstyle | 0x80000 | 0x20 | 0x80 | 0x08000000)
    except (AttributeError, OSError):
        pass


class CircleEditor:
    def __init__(self, root, area, radius, mem, me, win):
        self.root, self.area = root, area
        self.radius, self.mem, self.me, self.win = radius, mem, me, win
        self.hotkeys = Hotkeys()
        self.position = None
        self.read_error = ""
        self.message = "Move to a centre and press F2"
        self.saved = False

        root.title("SpiritVale circle area editor")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.90)
        self.canvas = tk.Canvas(root, width=PANEL_SIZE, height=PANEL_SIZE,
                                bg="#101820", highlightthickness=1,
                                highlightbackground="#55dfff")
        self.canvas.pack()
        # The top-level must be mapped once before WS_EX_NOACTIVATE is applied.
        # Styling an unmapped Tk window first leaves only a black surface over a
        # fullscreen game: the child canvas never reaches the compositor.
        root.update_idletasks()
        root.update()
        _click_through(root)
        self._place()
        # Draw before the first process read. If that read fails, an untouched
        # Tk canvas is just a black rectangle and gives the user no clue whether
        # F2 worked or the editor is alive.
        self._draw()
        root.after(POLL_MS, self.tick)

    def _place(self):
        """Pin the panel inside the game's upper-left corner."""
        try:
            x = int(self.win.left + 20)
            y = int(self.win.top + 50)
            self.root.geometry(f"{PANEL_SIZE}x{PANEL_SIZE}+{x}+{y}")
        except Exception:
            pass

    def _read_position(self):
        import memscan
        try:
            blob = self.mem.read(self.me + memscan.UNIT_POSITION, 12)
        except (OSError, ValueError, struct.error) as error:
            self.read_error = f"position read failed: {error}"
            return None
        if not blob:
            self.read_error = "position unreadable (relog or map change?)"
            return None
        try:
            x, _, z = struct.unpack("<fff", blob)
        except struct.error as error:
            self.read_error = f"bad position read: {error}"
            return None
        if not (math.isfinite(x) and math.isfinite(z)):
            self.read_error = "position was not finite"
            return None
        self.read_error = ""
        return x, z

    def _draw(self):
        c = self.canvas
        c.delete("all")
        position = self.position
        if position is None:
            c.create_text(PANEL_SIZE / 2, PANEL_SIZE / 2,
                          text=("PLAYER POSITION UNREADABLE\n\n"
                                + (self.read_error or "waiting for first read")),
                          fill="#ff7777", justify="center",
                          font=("Consolas", 15, "bold"))
            c.create_text(PANEL_SIZE / 2, PANEL_SIZE - 35,
                          text="Esc cancel   (do not run the bot while editing)",
                          fill="#ffffff", font=("Consolas", 10, "bold"))
            return

        transform = view_transform(self.area.circles, position,
                                   PANEL_SIZE, PANEL_SIZE, self.radius)
        for i, circle in enumerate(self.area.circles, 1):
            box = project_circle(circle, transform)
            colour = "#74ddff" if i < len(self.area.circles) else "#b5efff"
            c.create_oval(*box, outline=colour, width=3)
            sx = (box[0] + box[2]) / 2
            sy = (box[1] + box[3]) / 2
            c.create_text(sx, sy, text=str(i), fill=colour,
                          font=("Consolas", 10, "bold"))

        preview = (position[0], position[1], self.radius)
        c.create_oval(*project_circle(preview, transform), outline="#ffe45c",
                      width=3, dash=(8, 5))
        scale, ox, oy = transform
        px, py = ox + position[0] * scale, oy - position[1] * scale
        c.create_line(px - 8, py, px + 8, py, fill="#ffffff", width=2)
        c.create_line(px, py - 8, px, py + 8, fill="#ffffff", width=2)

        c.create_rectangle(8, 8, PANEL_SIZE - 8, 84,
                           fill="#101820", outline="#305060")
        c.create_text(18, 18, anchor="nw",
                      text=(f"AREA {self.area.name}   circles {len(self.area.circles)}"
                            f"   radius {self.radius:g}\n"
                            "F2 add   F3/F4 radius -/+   Backspace undo\n"
                            "End save   Esc cancel"),
                      fill="#ffffff", font=("Consolas", 11, "bold"))
        c.create_text(12, PANEL_SIZE - 12, anchor="sw", text=self.message,
                      fill="#ffe45c", font=("Consolas", 10, "bold"))

    def tick(self):
        self._place()
        self.position = self._read_position()
        if self.hotkeys.hit(F3):
            self.radius = max(MIN_RADIUS, self.radius - RADIUS_STEP)
            self.message = f"next circle radius {self.radius:g}"
        if self.hotkeys.hit(F4):
            self.radius += RADIUS_STEP
            self.message = f"next circle radius {self.radius:g}"
        if self.hotkeys.hit(F2):
            if self.position is None:
                self.message = "cannot add: player position unreadable"
            else:
                self.area.add_circle(self.position[0], self.position[1], self.radius)
                self.message = (f"added circle {len(self.area.circles)} at "
                                f"{self.position[0]:.1f},{self.position[1]:.1f}")
        if self.hotkeys.hit(BACKSPACE):
            if self.area.circles:
                self.area.circles.pop()
                self.message = f"removed last circle; {len(self.area.circles)} remain"
            else:
                self.message = "nothing to undo"
        if self.hotkeys.hit(ESCAPE):
            self.message = "cancelled -- nothing written"
            print(self.message)
            self.root.destroy()
            return
        if self.hotkeys.hit(END):
            if not self.area.circles:
                self.message = "add at least one circle before saving"
            elif self.area.save():
                self.saved = True
                print(f"saved {self.area.name!r}: {self.area.describe()} -> {AREA_FILE}")
                print(f"run it with: python minimap_bot.py --area {self.area.name}")
                self.root.destroy()
                return
            else:
                self.message = "COULD NOT SAVE areas.json"
        self._draw()
        self.root.after(POLL_MS, self.tick)


def run_editor(name, radius=50.0, replace=False):
    try:
        radius = float(radius)
    except (TypeError, ValueError):
        radius = 0.0
    if not name or not math.isfinite(radius) or radius <= 0.0:
        print("usage: python minimap_bot.py --place-circles <name> [radius] [--replace]")
        return False

    area = Area(name).load()
    if area.cells and not replace:
        print(f"area {name!r} is a walked mask; use another name or add --replace")
        return False
    if replace:
        area = Area(name)
    elif area.circles:
        print(f"editing {name!r}: {area.describe()} -- new circles will be added")

    import memscan
    print("finding your character -- the first heap sweep takes ~15 s, it is not hung",
          flush=True)
    mem = memscan.Mem()
    units = memscan.world_units(mem)
    me = memscan.local_player(mem, units[0][1]) if units else None
    if not me:
        print("no local player found -- is the character actually in the world?")
        return False
    win = find_window()
    print("overlay ready: F2 add, F3/F4 resize, Backspace undo, End save, Esc cancel")
    root = tk.Tk()
    CircleEditor(root, area, radius, mem, me, win)
    root.mainloop()
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--demo" in argv:
        circles = [(0.0, 0.0, 20.0), (25.0, 0.0, 20.0)]
        transform = view_transform(circles, (12.5, 0.0), 500, 500, 20.0)
        boxes = [project_circle(c, transform) for c in circles]
        assert boxes[0][2] > boxes[1][0], "overlapping world circles must overlap onscreen"
        assert all(0 <= v <= 500 for box in boxes for v in box)

        class _BadMem:
            def read(self, *_):
                raise OSError("demo read failure")

        class _Win:
            left = top = 0

        root = tk.Tk()
        root.withdraw()
        editor = CircleEditor(root, Area("demo"), 30.0, _BadMem(), 0, _Win())
        editor.tick()
        user32 = ctypes.windll.user32
        client = root.winfo_id()
        parent = user32.GetParent(client)
        assert not (user32.GetWindowLongW(client, -20) & 0x20), \
            "click-through must not be applied to Tk's drawable client HWND"
        assert user32.GetWindowLongW(parent, -20) & 0x20, \
            "Tk's native parent HWND must carry click-through"
        texts = [editor.canvas.itemcget(item, "text")
                 for item in editor.canvas.find_all()
                 if editor.canvas.type(item) == "text"]
        assert any("position read failed" in text for text in texts), texts
        root.destroy()
        print("area editor demo ok")
        return
    name = argv[0] if argv else None
    radius = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else 50.0
    run_editor(name, radius, "--replace" in argv)


if __name__ == "__main__":
    main()
