"""Read-only hotkey recorder for SpiritVale circle and polygon farming zones."""

import argparse
import ctypes
import json
import math
import os
import struct
import time

from farming_zone import detect_horizontal_axes, horizontal_point
from minimap_bot import AREA_FILE, Area


_KEY_NAMES = {"backspace": 0x08, "enter": 0x0D, "escape": 0x1B,
              "space": 0x20, "end": 0x23, "home": 0x24,
              "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
              "insert": 0x2D, "delete": 0x2E}
_KEY_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 13)})


def key_code(name):
    try:
        return _KEY_NAMES[str(name).strip().lower()]
    except KeyError as error:
        raise ValueError(f"unsupported hotkey {name!r}") from error


class Hotkeys:
    def __init__(self, mapping, get_state=None):
        self.mapping = {action: key_code(name) for action, name in mapping.items()}
        if len(set(self.mapping.values())) != len(self.mapping):
            raise ValueError("zone recorder hotkeys must be different")
        self.get_state = get_state or ctypes.windll.user32.GetAsyncKeyState
        self.down = {}

    def hit(self, action):
        key = self.mapping[action]
        down = bool(self.get_state(key) & 0x8000)
        hit = down and not self.down.get(key, False)
        self.down[key] = down
        return hit


class RecordingSession:
    """Store raw XYZ reads until the horizontal plane can be detected."""

    def __init__(self, shape, radius=None):
        if shape not in ("circle", "polygon"):
            raise ValueError("shape must be circle or polygon")
        if shape == "circle" and (radius is None or not math.isfinite(float(radius))
                                  or float(radius) <= 0.0):
            raise ValueError("circle radius must be finite and positive")
        self.shape = shape
        self.radius = float(radius) if radius is not None else None
        self.recording = False
        self.samples = []
        self.points = []
        self.center = None

    def start(self, position):
        position = tuple(map(float, position))
        self.recording = True
        self.samples = [position]
        self.points = []
        self.center = position if self.shape == "circle" else None

    def sample(self, position):
        if self.recording:
            self.samples.append(tuple(map(float, position)))

    def add(self, position):
        if not self.recording or self.shape != "polygon":
            raise ValueError("start polygon recording before adding points")
        position = tuple(map(float, position))
        self.points.append(position)
        self.samples.append(position)
        return len(self.points)

    def undo(self):
        if self.shape == "polygon" and self.points:
            return self.points.pop()
        if self.shape == "circle" and self.center is not None:
            old, self.center = self.center, None
            return old
        return None

    def finish(self):
        if not self.recording:
            raise ValueError("recording has not started")
        axes = detect_horizontal_axes(self.samples)
        if axes != "xz":
            raise ValueError("detected X/Y ground, but SpiritVale movement is X/Z; "
                             "zone was not saved")
        if self.shape == "circle":
            if self.center is None:
                raise ValueError("circle center was undone; press Start again")
            return axes, horizontal_point(self.center, axes), self.radius
        if len(self.points) < 3:
            raise ValueError("a polygon needs at least three recorded points")
        return axes, tuple(horizontal_point(point, axes) for point in self.points)


def _read_position(mem, me, offset):
    blob = mem.read(me + offset, 12)
    if not blob:
        return None
    position = struct.unpack("<fff", blob)
    return (position if all(math.isfinite(value) and -20000.0 <= value <= 20000.0
                            for value in position) else None)


def clear_zone(name, path=AREA_FILE):
    blob = Area._blob(path)
    areas = blob.get("areas", {})
    if name not in areas:
        print(f"zone {name!r} does not exist")
        return False
    del areas[name]
    try:
        with open(path, "w") as handle:
            json.dump(blob, handle)
    except OSError as error:
        print(f"zone: could not clear {name!r}: {error}")
        return False
    print(f"zone: cleared {name!r} from {path}")
    return True


def _find_local_player(mem, ms):
    """Resolve stale class slots, sweep units, and try several valid seeds."""
    classes = ms.type_classes(mem)
    if not classes.get("monster"):
        print("zone: memory class cache is stale after a game update")
        print("zone: one-time class recovery can take 2-4 minutes; please wait",
              flush=True)
        wanted = {"monster": ms.CLASS_NAMES["monster"]}
        found = ms.find_classes(
            mem, wanted=wanted,
            progress=lambda message: print(f"zone: {message}; locating live objects",
                                           flush=True))
        classes = dict(classes, **found)
        monster = classes.get("monster")
        if not monster:
            print("zone: MonsterController could not be recovered from this game build")
            return None
        rva = ms.class_slot_rva(mem, monster)
        if rva:
            cached = ms.load_rva_cache()
            cached["monster"] = rva
            ms.save_rva_cache(cached)
            print(f"zone: recovered monster class and cached RVA 0x{rva:X}")
        else:
            print("zone: recovered monster class for this run (cache slot not found)")

    print("zone: scanning unit list (first sweep can take about 15 s)", flush=True)
    units = ms.world_units(mem, classes=classes)
    seen = set()
    for _, unit, *_ in units:
        if unit in seen:
            continue
        seen.add(unit)
        me = ms.local_player(mem, unit)
        if me:
            return me
        if len(seen) >= 64:
            break
    print(f"zone: scanned {len(units)} unit objects but could not resolve the "
          "local connection")
    return None


def record_zone(shape, name, radius=50.0, replace=False, keys=None,
                path=AREA_FILE, poll=0.05):
    existing = Area(name, path=path).load()
    if existing.defined and not replace:
        print(f"zone {name!r} already exists; use --replace or another name")
        return False
    session = RecordingSession(shape, radius if shape == "circle" else None)
    key_names = keys or {"start": "f6", "add": "f7", "finish": "f8",
                         "undo": "backspace", "cancel": "escape"}
    hotkeys = Hotkeys(key_names)

    import memscan
    print("zone: finding local player from read-only game memory", flush=True)
    mem = memscan.Mem()
    try:
        me = _find_local_player(mem, memscan)
        if not me:
            print("zone: no local player found; wait for loading/relogin to finish, "
                  "then retry")
            return False
        labels = ", ".join(f"{action}={key_names[action]}"
                           for action in ("start", "add", "finish", "undo", "cancel"))
        print(f"zone recorder ready ({shape}, {labels})")
        print("zone: positions come from read-only game memory; no overlay is used")
        while True:
            if hotkeys.hit("cancel"):
                print("\nzone: cancelled; nothing written")
                return False
            position = _read_position(mem, me, memscan.UNIT_POSITION)
            if position is None:
                print("\rzone: player position unreadable (relog/map change?)", end="",
                      flush=True)
                time.sleep(poll)
                continue
            session.sample(position)
            if hotkeys.hit("start"):
                session.start(position)
                if shape == "circle":
                    print(f"\nzone: recording started; circle center raw XYZ "
                          f"{position[0]:.2f},{position[1]:.2f},{position[2]:.2f}")
                else:
                    print("\nzone: polygon recording started; move and press Add")
            if hotkeys.hit("add"):
                try:
                    count = session.add(position)
                    print(f"\nzone: point {count} raw XYZ "
                          f"{position[0]:.2f},{position[1]:.2f},{position[2]:.2f}")
                except ValueError as error:
                    print(f"\nzone: {error}")
            if hotkeys.hit("undo"):
                removed = session.undo()
                print("\nzone: " + ("undid last point/center" if removed else "nothing to undo"))
            if hotkeys.hit("finish"):
                try:
                    result = session.finish()
                    axes = result[0]
                    area = Area(name, path=path, axes=axes)
                    if shape == "circle":
                        _, center, saved_radius = result
                        area.set_circle(center[0], center[1], saved_radius)
                    else:
                        _, points = result
                        area.set_polygon(points, axes)
                    if not area.save():
                        print(f"\nzone: could not write {path}")
                        return False
                    print(f"\nzone: detected horizontal axes {axes.upper()}")
                    print(f"zone: saved {name!r}: {area.describe()} -> {path}")
                    print(f"zone: enable with python minimap_bot.py --area {name}")
                    return True
                except ValueError as error:
                    print(f"\nzone: cannot finish: {error}")
            time.sleep(poll)
    finally:
        mem.close()


def interactive_record(path=AREA_FILE, input_fn=input, recorder=None):
    """Guided `--record` flow; an existing name is replaced deliberately."""
    recorder = recorder or record_zone
    print("\nSpiritVale farming-zone recorder")
    print("  [1] POLYGON  mark at least 3 boundary points")
    print("  [2] CIRCLE   stand at the center and choose a radius")
    try:
        while True:
            choice = input_fn("Select mode [1=polygon, 2=circle]: ").strip().lower()
            if choice in ("1", "p", "polygon"):
                shape = "polygon"
                break
            if choice in ("2", "c", "circle"):
                shape = "circle"
                break
            print("Please enter 1 for polygon or 2 for circle.")

        while True:
            name = input_fn("Area name: ").strip()
            if name:
                break
            print("Area name cannot be blank.")

        radius = None
        if shape == "circle":
            while True:
                raw = input_fn("Circle radius [50]: ").strip() or "50"
                try:
                    radius = float(raw)
                except ValueError:
                    radius = 0.0
                if math.isfinite(radius) and radius > 0.0:
                    break
                print("Radius must be a positive number.")
    except (EOFError, KeyboardInterrupt):
        print("\nzone: cancelled; nothing written")
        return False

    existing = Area(name, path=path).load()
    if existing.defined:
        print(f"zone: {name!r} already exists ({existing.describe()})")
        print("zone: it will be replaced automatically when recording finishes")
    else:
        print(f"zone: creating new {shape} area {name!r}")
    return recorder(shape, name, radius=radius, replace=True, path=path)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for shape in ("circle", "polygon"):
        item = sub.add_parser(shape)
        item.add_argument("name")
        if shape == "circle":
            item.add_argument("--radius", type=float, default=50.0)
        item.add_argument("--replace", action="store_true")
        item.add_argument("--start-key", default="f6")
        item.add_argument("--add-key", default="f7")
        item.add_argument("--finish-key", default="f8")
        item.add_argument("--undo-key", default="backspace")
        item.add_argument("--cancel-key", default="escape")
    clear = sub.add_parser("clear")
    clear.add_argument("name")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "clear":
        return 0 if clear_zone(args.name) else 1
    keys = {"start": args.start_key, "add": args.add_key,
            "finish": args.finish_key, "undo": args.undo_key,
            "cancel": args.cancel_key}
    try:
        ok = record_zone(args.command, args.name,
                         getattr(args, "radius", None), args.replace, keys)
    except (OSError, ValueError) as error:
        print(f"zone: {error}")
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
