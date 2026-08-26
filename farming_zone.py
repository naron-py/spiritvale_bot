"""Pure world-coordinate farming-zone geometry.

No process reads, screenshots, controller calls, or game-specific offsets live here.
The bot and recorder adapt their X/Y/Z memory tuples to these horizontal 2D zones.
"""

from dataclasses import dataclass
import math
import random

_EPS = 1e-7


def _point(value):
    if len(value) != 2:
        raise ValueError("a zone point needs two horizontal coordinates")
    point = float(value[0]), float(value[1])
    if not all(math.isfinite(v) for v in point):
        raise ValueError("zone coordinates must be finite")
    return point


def _distance_to_segment(point, a, b):
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    size = dx * dx + dy * dy
    if size <= _EPS:
        return math.hypot(px - ax, py - ay), a
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / size))
    hit = ax + t * dx, ay + t * dy
    return math.hypot(px - hit[0], py - hit[1]), hit


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _segment_breaks(start, end, a, b):
    """Movement parameters where start->end meets one polygon edge."""
    move = end[0] - start[0], end[1] - start[1]
    edge = b[0] - a[0], b[1] - a[1]
    offset = a[0] - start[0], a[1] - start[1]
    denominator = _cross(move, edge)
    if abs(denominator) > _EPS:
        t = _cross(offset, edge) / denominator
        u = _cross(offset, move) / denominator
        if -_EPS <= t <= 1 + _EPS and -_EPS <= u <= 1 + _EPS:
            return [max(0.0, min(1.0, t))]
        return []
    if abs(_cross(offset, move)) > _EPS:
        return []
    size = move[0] * move[0] + move[1] * move[1]
    if size <= _EPS:
        return []
    out = []
    for point in (a, b):
        t = ((point[0] - start[0]) * move[0]
             + (point[1] - start[1]) * move[1]) / size
        if -_EPS <= t <= 1 + _EPS:
            out.append(max(0.0, min(1.0, t)))
    return out


def _segments_distance(a, b, c, d):
    if _segment_breaks(a, b, c, d):
        return 0.0
    return min(_distance_to_segment(a, c, d)[0],
               _distance_to_segment(b, c, d)[0],
               _distance_to_segment(c, a, b)[0],
               _distance_to_segment(d, a, b)[0])


@dataclass(frozen=True)
class CircleZone:
    center: tuple
    radius: float

    def __post_init__(self):
        object.__setattr__(self, "center", _point(self.center))
        radius = float(self.radius)
        if not math.isfinite(radius) or radius <= 0.0:
            raise ValueError("circle radius must be finite and positive")
        object.__setattr__(self, "radius", radius)

    def contains(self, point, margin=0.0):
        x, y = _point(point)
        radius = max(0.0, self.radius - max(0.0, float(margin)))
        return math.hypot(x - self.center[0], y - self.center[1]) <= radius + _EPS

    def nearest_safe(self, point, margin=0.0):
        point = _point(point)
        if self.contains(point, margin):
            return point
        radius = max(0.0, self.radius - max(0.0, float(margin)))
        dx, dy = point[0] - self.center[0], point[1] - self.center[1]
        distance = math.hypot(dx, dy)
        if distance <= _EPS or radius <= _EPS:
            return self.center
        scale = radius / distance
        return self.center[0] + dx * scale, self.center[1] + dy * scale

    def guard_step(self, current, proposed, margin=0.0):
        proposed = _point(proposed)
        if self.contains(proposed, margin):
            return True, proposed
        return False, self.nearest_safe(proposed, margin)

    def segment_interval(self, current, proposed, margin=0.0):
        """Parameter interval of current->proposed covered by this safe disc."""
        current, proposed = _point(current), _point(proposed)
        radius = max(0.0, self.radius - max(0.0, float(margin)))
        dx, dy = proposed[0] - current[0], proposed[1] - current[1]
        fx, fy = current[0] - self.center[0], current[1] - self.center[1]
        a = dx * dx + dy * dy
        if a <= _EPS:
            return (0.0, 1.0) if self.contains(current, margin) else None
        b = 2.0 * (fx * dx + fy * dy)
        c = fx * fx + fy * fy - radius * radius
        discriminant = b * b - 4.0 * a * c
        if discriminant < -_EPS:
            return None
        root = math.sqrt(max(0.0, discriminant))
        low = max(0.0, (-b - root) / (2.0 * a))
        high = min(1.0, (-b + root) / (2.0 * a))
        return (low, high) if low <= high + _EPS else None

    def random_safe(self, margin=0.0, rng=random):
        radius = max(0.0, self.radius - max(0.0, float(margin)))
        distance = radius * math.sqrt(rng.random())
        angle = math.tau * rng.random()
        return (self.center[0] + distance * math.cos(angle),
                self.center[1] + distance * math.sin(angle))

    def bounds(self):
        x, y = self.center
        return (x - self.radius, y - self.radius), (x + self.radius, y + self.radius)


class PolygonZone:
    def __init__(self, points):
        points = tuple(_point(point) for point in points)
        if len(points) > 1 and points[0] == points[-1]:
            points = points[:-1]
        if len(set(points)) < 3:
            raise ValueError("a polygon needs at least three different points")
        area2 = sum(a[0] * b[1] - b[0] * a[1]
                    for a, b in zip(points, points[1:] + points[:1]))
        if abs(area2) <= _EPS:
            raise ValueError("polygon points enclose no area")
        self.points = points
        self._ccw = area2 > 0.0

    def _edges(self):
        return zip(self.points, self.points[1:] + self.points[:1])

    def _base_contains(self, point):
        x, y = point
        inside = False
        for a, b in self._edges():
            distance, _ = _distance_to_segment(point, a, b)
            if distance <= _EPS:
                return True
            if (a[1] > y) != (b[1] > y):
                cross_x = (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]
                if x < cross_x:
                    inside = not inside
        return inside

    def contains(self, point, margin=0.0):
        point = _point(point)
        if not self._base_contains(point):
            return False
        margin = max(0.0, float(margin))
        if margin <= _EPS:
            return True
        return min(_distance_to_segment(point, a, b)[0]
                   for a, b in self._edges()) + _EPS >= margin

    def _centroid(self):
        cross_sum = cx = cy = 0.0
        for a, b in self._edges():
            cross = a[0] * b[1] - b[0] * a[1]
            cross_sum += cross
            cx += (a[0] + b[0]) * cross
            cy += (a[1] + b[1]) * cross
        if abs(cross_sum) <= _EPS:
            return self.points[0]
        return cx / (3.0 * cross_sum), cy / (3.0 * cross_sum)

    def _safe_candidates(self, point, margin):
        candidates = [self._centroid(),
                      (sum(p[0] for p in self.points) / len(self.points),
                       sum(p[1] for p in self.points) / len(self.points))]
        shift = max(0.0, margin)
        for a, b in self._edges():
            _, projected = _distance_to_segment(point, a, b)
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length <= _EPS:
                continue
            if self._ccw:
                nx, ny = -dy / length, dx / length
            else:
                nx, ny = dy / length, -dx / length
            for base in (projected, ((a[0] + b[0]) / 2,
                                      (a[1] + b[1]) / 2)):
                candidates.append((base[0] + nx * shift,
                                   base[1] + ny * shift))
        return [candidate for candidate in candidates
                if self.contains(candidate, margin)]

    def nearest_safe(self, point, margin=0.0):
        point = _point(point)
        margin = max(0.0, float(margin))
        if self.contains(point, margin):
            return point
        candidates = self._safe_candidates(point, margin)
        if not candidates:
            # Concave polygons can have their centroid outside, and a safety
            # margin wider than a narrow corridor can erase that corridor. A
            # bounded grid supplies a valid interior fallback without a geometry
            # dependency; return-to-zone is rare, so this is off the hot path.
            (x0, y0), (x1, y1) = self.bounds()
            for iy in range(1, 32):
                y = y0 + (y1 - y0) * iy / 32
                for ix in range(1, 32):
                    candidate = (x0 + (x1 - x0) * ix / 32, y)
                    if self.contains(candidate, margin):
                        candidates.append(candidate)
        if not candidates and margin > 0.0:
            return self.nearest_safe(point, 0.0)
        if not candidates:
            return self.points[0]
        return min(candidates,
                   key=lambda p: (p[0] - point[0]) ** 2 + (p[1] - point[1]) ** 2)

    def guard_step(self, current, proposed, margin=0.0):
        current, proposed = _point(current), _point(proposed)
        if self.segment_safe(current, proposed, margin):
            return True, proposed
        if self.contains(current, margin) and self.contains(proposed, margin):
            return False, current
        return False, self.nearest_safe(proposed, margin)

    def segment_safe(self, current, proposed, margin=0.0):
        current, proposed = _point(current), _point(proposed)
        margin = max(0.0, float(margin))
        if not self.contains(current, margin) or not self.contains(proposed, margin):
            return False
        breaks = {0.0, 1.0}
        for a, b in self._edges():
            breaks.update(_segment_breaks(current, proposed, a, b))
            if (margin > _EPS
                    and _segments_distance(current, proposed, a, b) + _EPS < margin):
                return False
        ordered = sorted(breaks)
        for left, right in zip(ordered, ordered[1:]):
            t = (left + right) / 2.0
            point = (current[0] + (proposed[0] - current[0]) * t,
                     current[1] + (proposed[1] - current[1]) * t)
            if not self.contains(point, margin):
                return False
        return True

    def random_safe(self, margin=0.0, rng=random):
        (x0, y0), (x1, y1) = self.bounds()
        for _ in range(1000):
            point = rng.uniform(x0, x1), rng.uniform(y0, y1)
            if self.contains(point, margin):
                return point
        return self.nearest_safe(self._centroid(), margin)

    def bounds(self):
        return ((min(p[0] for p in self.points), min(p[1] for p in self.points)),
                (max(p[0] for p in self.points), max(p[1] for p in self.points)))


def filter_targets(targets, zone, position, eligible=lambda target: True):
    """Preserve input order while rejecting ineligible or out-of-zone entities."""
    return [target for target in targets
            if eligible(target) and zone.contains(position(target))]


def detect_horizontal_axes(samples, default="xz"):
    """Choose X/Y or X/Z from movement; the less-changing axis is vertical."""
    samples = [tuple(map(float, sample)) for sample in samples]
    if len(samples) < 2:
        return default
    y_range = max(p[1] for p in samples) - min(p[1] for p in samples)
    z_range = max(p[2] for p in samples) - min(p[2] for p in samples)
    if max(y_range, z_range) <= 1e-3:
        return default
    return "xz" if z_range >= y_range else "xy"


def horizontal_point(position, axes):
    x, y, z = position
    if axes == "xz":
        return float(x), float(z)
    if axes == "xy":
        return float(x), float(y)
    raise ValueError(f"unsupported horizontal axes {axes!r}")
