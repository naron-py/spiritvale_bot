"""Cached-snapshot QGraphicsView world renderer."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QPainter, QPainterPath, QPen,
                           QPolygonF, QTransform)
from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem,
                               QGraphicsPathItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView)

from ..model import BotSnapshot
from ..theme import AMBER, BLUE, BORDER, CYAN, GREEN, MUTED, RED, TEXT


class WorldView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.follow_player = False
        self.last_sequence = -1
        self._snapshot = None
        self._draft = ()
        self._auto_fit = True
        self._scale = 1.0
        self.marker_states = ()

    def update_snapshot(self, snapshot: BotSnapshot):
        if snapshot.sequence < self.last_sequence:
            return
        self.last_sequence = snapshot.sequence
        self._snapshot = snapshot
        self._redraw()

    def set_draft(self, points):
        self._draft = tuple((float(x), float(z)) for x, z in points)
        if self._snapshot is not None:
            self._redraw()

    @staticmethod
    def _point(point):
        return QPointF(point[0], -point[1])

    def _bounds(self, snapshot):
        points = []
        if snapshot.player:
            points.append(snapshot.player)
        points += [(item.x, item.z) for item in snapshot.entities]
        points += list(snapshot.zone.points) + list(snapshot.path) + list(snapshot.trail)
        points += list(self._draft)
        for x, z, radius in snapshot.zone.circles:
            points.extend(((x - radius, z - radius), (x + radius, z + radius)))
        if not points:
            points = [(0, 0), (100, 100)]
        xs, zs = [p[0] for p in points], [p[1] for p in points]
        pad = max(8.0, max(max(xs) - min(xs), max(zs) - min(zs)) * 0.08)
        return QRectF(min(xs) - pad, -(max(zs) + pad),
                      max(xs) - min(xs) + 2 * pad,
                      max(zs) - min(zs) + 2 * pad)

    def _redraw(self):
        snapshot = self._snapshot
        if snapshot is None:
            return
        scene = self.scene()
        scene.clear()
        bounds = self._bounds(snapshot)
        scene.setSceneRect(bounds)
        self._draw_grid(bounds)
        self._draw_zone(snapshot)
        self._draw_path(snapshot.trail, QColor(BLUE), dotted=False, width=2.0)
        self._draw_path(snapshot.path, QColor(CYAN), dotted=True, width=2.4)
        self._draw_entities(snapshot)
        self._draw_draft()
        if self._auto_fit:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
            self._scale = self.transform().m11()
            # Follow mode needs one fitted scale before it begins centring on
            # the player. It must not disable that first fit while the view is
            # still empty (the default setting enables Follow Player).
            if self.follow_player:
                self._auto_fit = False
        elif self.follow_player and snapshot.player:
            self.centerOn(self._point(snapshot.player))

    def _draw_grid(self, bounds):
        scene = self.scene()
        span = max(bounds.width(), bounds.height())
        step = 5.0 if span < 80 else 10.0 if span < 220 else 25.0
        left = math.floor(bounds.left() / step) * step
        right = math.ceil(bounds.right() / step) * step
        top = math.floor(bounds.top() / step) * step
        bottom = math.ceil(bounds.bottom() / step) * step
        minor = QPen(QColor(17, 48, 66, 150), 0)
        major = QPen(QColor(24, 66, 88, 190), 0)
        value = left
        index = 0
        while value <= right + 0.01:
            scene.addLine(value, top, value, bottom, major if index % 5 == 0 else minor)
            value += step
            index += 1
        value = top
        index = 0
        while value <= bottom + 0.01:
            scene.addLine(left, value, right, value, major if index % 5 == 0 else minor)
            value += step
            index += 1

    def _draw_zone(self, snapshot):
        zone = snapshot.zone
        scene = self.scene()
        if zone.points and zone.kind == "polygon":
            polygon = QPolygonF([self._point(point) for point in zone.points])
            item = scene.addPolygon(polygon, QPen(QColor(CYAN), 1.4),
                                    QBrush(QColor(13, 184, 202, 42)))
            item.setZValue(1)
            for index, point in enumerate(zone.points, 1):
                p = self._point(point)
                scene.addEllipse(p.x() - 2.4, p.y() - 2.4, 4.8, 4.8,
                                 QPen(QColor(CYAN), 1), QBrush(QColor("#07131f")))
                label = scene.addSimpleText(str(index))
                label.setBrush(QColor(TEXT))
                label.setScale(0.55)
                label.setPos(p.x() + 2.5, p.y() - 6)
                label.setZValue(4)
        elif zone.kind == "circles":
            for x, z, radius in zone.circles:
                scene.addEllipse(x - radius, -z - radius, radius * 2, radius * 2,
                                 QPen(QColor(CYAN), 1.4),
                                 QBrush(QColor(13, 184, 202, 35)))
        elif zone.kind == "cells":
            for x, z in zone.points[:1500]:
                scene.addRect(x - 1.5, -z - 1.5, 3.0, 3.0,
                              QPen(Qt.NoPen), QBrush(QColor(13, 184, 202, 30)))

    def _draw_path(self, points, color, dotted, width):
        if len(points) < 2:
            return
        path = QPainterPath(self._point(points[0]))
        for point in points[1:]:
            path.lineTo(self._point(point))
        pen = QPen(color, width)
        pen.setCosmetic(True)
        if dotted:
            pen.setStyle(Qt.DotLine)
        item = self.scene().addPath(path, pen)
        item.setZValue(2)

    def _dot(self, x, z, radius, fill, outline, dashed=False, zvalue=5):
        p = self._point((x, z))
        pen = QPen(QColor(outline), 1.5)
        pen.setCosmetic(True)
        if dashed:
            pen.setStyle(Qt.DashLine)
        item = self.scene().addEllipse(p.x() - radius, p.y() - radius,
                                      radius * 2, radius * 2, pen,
                                      QBrush(QColor(fill)))
        item.setZValue(zvalue)
        return item

    def _draw_entities(self, snapshot):
        marker_states = []
        for entity in snapshot.entities:
            if entity.kind == "player":
                self._dot(entity.x, entity.z, 3.2, BLUE, "#b8ddff")
            elif entity.kind == "monster" and entity.valid_monster:
                self._dot(entity.x, entity.z, 3.6, "#2a5b16", GREEN)
                marker_states.append("green")
            elif entity.kind == "monster":
                self._dot(entity.x, entity.z, 3.8, "#340d12", RED, dashed=True)
                marker_states.append("red")
            if entity.kind == "monster" and entity.current:
                p = self._point((entity.x, entity.z))
                ring = self.scene().addEllipse(
                    p.x() - 5.5, p.y() - 5.5, 11.0, 11.0,
                    QPen(QColor("#ffffff"), 1.8), QBrush(Qt.NoBrush))
                ring.setZValue(9)
                marker_states.append("target-ring")
        self.marker_states = tuple(marker_states)
        if snapshot.player is not None:
            self._dot(snapshot.player[0], snapshot.player[1], 4.2,
                      BLUE, "#b9e5ff", zvalue=10)

    def _draw_draft(self):
        if not self._draft:
            return
        scene = self.scene()
        pen = QPen(QColor(AMBER), 2)
        pen.setCosmetic(True)
        for index, point in enumerate(self._draft, 1):
            p = self._point(point)
            scene.addEllipse(p.x() - 3, p.y() - 3, 6, 6, pen,
                             QBrush(QColor("#5b3c05")))
            label = scene.addSimpleText(str(index))
            label.setBrush(QColor(AMBER))
            label.setScale(0.65)
            label.setPos(p.x() + 3, p.y() - 8)
        if len(self._draft) > 1:
            self._draw_path(self._draft, QColor(AMBER), dotted=True, width=2)

    def fit_zone(self):
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
        self._scale = self.transform().m11()
        self._auto_fit = not self.follow_player

    def set_follow_player(self, enabled):
        self.follow_player = bool(enabled)
        if self._snapshot is None:
            self._auto_fit = True
        elif enabled:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
            self._scale = self.transform().m11()
            self._auto_fit = False
            if self._snapshot.player:
                self.centerOn(self._point(self._snapshot.player))
        else:
            self._auto_fit = True
            self.fit_zone()

    def zoom_in(self):
        self._auto_fit = False
        self.scale(1.25, 1.25)
        self._scale = self.transform().m11()

    def zoom_out(self):
        self._auto_fit = False
        self.scale(0.8, 0.8)
        self._scale = self.transform().m11()

    def wheelEvent(self, event):
        self._auto_fit = False
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        self._scale = self.transform().m11()
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The first snapshot can arrive while the stacked page is still being
        # laid out. Refit after the viewport receives its real dimensions or the
        # scene remains at roughly one world unit per pixel in a tiny centre box.
        if self._auto_fit and self._snapshot is not None:
            self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)
            self._scale = self.transform().m11()
