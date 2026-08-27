"""Polygon draft validation and atomic persistence in areas.json format."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
from typing import Iterable

from .model import SnapshotError, ZoneSnapshot


class ZoneError(ValueError):
    pass


def _point(value) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ZoneError("a zone point needs x and z")
    try:
        point = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise ZoneError("zone coordinates must be numbers") from exc
    if not all(math.isfinite(item) and abs(item) <= 10_000_000 for item in point):
        raise ZoneError("zone coordinates must be finite and plausible")
    return point


def _orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def _intersects(a, b, c, d):
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    eps = 1e-9
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and \
       ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
        return True
    return ((abs(o1) <= eps and _on_segment(a, b, c))
            or (abs(o2) <= eps and _on_segment(a, b, d))
            or (abs(o3) <= eps and _on_segment(c, d, a))
            or (abs(o4) <= eps and _on_segment(c, d, b)))


class ZoneDraft:
    def __init__(self, name: str = "", points: Iterable = ()):
        self.name = str(name).strip()
        self.points = [_point(point) for point in points]

    def add(self, point) -> tuple[float, float]:
        point = _point(point)
        if self.points and point == self.points[-1]:
            raise ZoneError("duplicate consecutive point")
        self.points.append(point)
        return point

    def undo(self):
        return self.points.pop() if self.points else None

    def clear(self):
        self.points.clear()

    def validate(self) -> tuple[tuple[float, float], ...]:
        if not self.name:
            raise ZoneError("zone name is required")
        points = tuple(self.points[:-1] if len(self.points) > 1
                       and self.points[0] == self.points[-1] else self.points)
        if len(set(points)) < 3:
            raise ZoneError("a polygon needs at least three different points")
        edges = list(zip(points, points[1:] + points[:1]))
        for i, (a, b) in enumerate(edges):
            for j, (c, d) in enumerate(edges):
                if j <= i or j in (i - 1, i + 1) or (i == 0 and j == len(edges) - 1):
                    continue
                if _intersects(a, b, c, d):
                    raise ZoneError("polygon edges self-intersect")
        area2 = sum(a[0] * b[1] - b[0] * a[1]
                    for a, b in edges)
        if abs(area2) <= 1e-7:
            raise ZoneError("polygon points enclose no area")
        return points


class ZoneStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.backup = self.path.with_suffix(self.path.suffix + ".bak")
        self.temp = self.path.with_suffix(self.path.suffix + ".tmp")
        self.last_warning = ""
        self._last_valid = None

    def names(self) -> list[str]:
        return sorted(self._load()["areas"])

    @staticmethod
    def _validated(data):
        if not isinstance(data, dict) or not isinstance(data.get("areas"), dict):
            raise ZoneError("area file has an invalid structure")
        return data

    def _read(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ZoneError(f"could not read area file: {exc}") from exc
        return self._validated(data)

    def _load(self):
        self.last_warning = ""
        if not self.path.exists():
            data = {"cell": 3.0, "areas": {}}
            self._last_valid = data
            return data
        try:
            data = self._read(self.path)
        except ZoneError as primary:
            if self.backup.exists():
                try:
                    data = self._read(self.backup)
                    self.last_warning = (
                        f"Primary area file invalid; loaded backup ({primary}).")
                except ZoneError:
                    data = None
            else:
                data = None
            if data is None and self._last_valid is not None:
                self.last_warning = (
                    f"Area reload failed; preserving last valid data ({primary}).")
                data = self._last_valid
            if data is None:
                raise primary
        self._last_valid = json.loads(json.dumps(data))
        return data

    def load_selected(self, preferred: str = "") -> ZoneSnapshot:
        data = self._load()
        areas = data["areas"]
        name = str(data.get("selected_area", "") or preferred).strip()
        if not name:
            return ZoneSnapshot()
        raw = areas.get(name)
        if not isinstance(raw, dict):
            raise ZoneError(f"saved zone {name!r} does not exist")
        axes = str(raw.get("axes", "xz")).lower()
        if axes != "xz":
            raise ZoneError(f"saved zone {name!r} uses unsupported {axes.upper()} axes")
        mapping = {"name": name, "kind": "none"}
        polygon = raw.get("polygon")
        if polygon is None and raw.get("shape") == "polygon":
            polygon = raw.get("points")
        if polygon:
            mapping.update(kind="polygon", points=polygon)
        elif raw.get("shape") == "circle" or (
                raw.get("center") is not None and raw.get("radius") is not None):
            center = raw.get("center")
            if not isinstance(center, (list, tuple)) or len(center) != 2:
                raise ZoneError(f"saved circle {name!r} has an invalid centre")
            mapping.update(kind="circles",
                           circles=[[center[0], center[1], raw.get("radius")]])
        elif raw.get("circles"):
            mapping.update(kind="circles", circles=raw["circles"])
        elif raw.get("cells"):
            cell = float(data.get("cell", 3.0))
            mapping.update(
                kind="cells", cell_size=cell,
                points=[[(float(x) + 0.5) * cell, (float(z) + 0.5) * cell]
                        for x, z in raw["cells"]],
            )
        try:
            zone = ZoneSnapshot.from_mapping(mapping)
        except (SnapshotError, TypeError, ValueError) as exc:
            raise ZoneError(f"saved zone {name!r} is invalid: {exc}") from exc
        if not zone.valid:
            raise ZoneError(f"saved zone {name!r} is invalid")
        return zone

    def select(self, name: str) -> None:
        name = str(name).strip()
        data = self._load()
        if name and name not in data["areas"]:
            raise ZoneError(f"saved zone {name!r} does not exist")
        data["selected_area"] = name
        self._write(data)

    def save_polygon(self, name: str, points: Iterable, *, select=False) -> None:
        draft = ZoneDraft(name, points)
        valid = draft.validate()
        data = self._load()
        data["areas"][draft.name] = {
            "shape": "polygon",
            "axes": "xz",
            "points": [[x, z] for x, z in valid],
        }
        if select:
            data["selected_area"] = draft.name
        self._write(data)

    def _write(self, data) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, separators=(",", ":")) + "\n"
        try:
            with self.temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                shutil.copy2(self.path, self.backup)
            os.replace(self.temp, self.path)
        except OSError as exc:
            self.temp.unlink(missing_ok=True)
            raise ZoneError(f"could not save area: {exc}") from exc
