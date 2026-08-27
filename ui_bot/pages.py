"""Main content pages. All update methods consume immutable snapshots only."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSizePolicy, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .config import UiSettings
from .model import (BotSnapshot, FailureCode, FAILURE_POLICIES,
                    ZoneDisplayState)
from .widgets.world_view import WorldView


def panel(parent=None):
    frame = QFrame(parent)
    frame.setObjectName("panel")
    return frame


def section(text):
    label = QLabel(text)
    label.setObjectName("section")
    return label


class StatCard(QFrame):
    def __init__(self, caption, icon, accent="cyan", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        badge = QLabel(icon)
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(34, 34)
        badge.setStyleSheet("font-size:20px; border:1px solid #1e728d; border-radius:17px; color:#16d9f4;")
        texts = QVBoxLayout()
        caption_label = QLabel(caption.upper())
        caption_label.setObjectName("muted")
        caption_label.setStyleSheet("font-size:9px")
        self.value = QLabel("—")
        self.value.setObjectName(accent)
        self.value.setWordWrap(False)
        texts.addWidget(caption_label)
        texts.addWidget(self.value)
        layout.addWidget(badge)
        layout.addLayout(texts, 1)
        self.setMinimumHeight(52)


class SnapshotPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.update_count = 0

    def apply_snapshot(self, snapshot: BotSnapshot):
        self.update_count += 1


class DashboardPage(SnapshotPage):
    record_requested = Signal()
    add_position_requested = Signal()
    undo_requested = Signal()
    save_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(7)
        cards = QHBoxLayout()
        cards.setSpacing(6)
        self.state_card = StatCard("Bot state", "♞", "cyan")
        self.player_card = StatCard("Player", "●", "cyan")
        self.target_card = StatCard("Target", "◎", "green")
        self.zone_card = StatCard("In zone", "♣", "green")
        for card in (self.state_card, self.player_card, self.target_card, self.zone_card):
            cards.addWidget(card, 1)
        root.addLayout(cards)

        body = QHBoxLayout()
        body.setSpacing(7)
        world_panel = panel()
        world_layout = QVBoxLayout(world_panel)
        world_layout.setContentsMargins(9, 7, 9, 8)
        world_layout.setSpacing(5)
        world_layout.addWidget(section("LIVE WORLD VIEW"))
        self.world_view = WorldView()
        self.world_view.setMinimumHeight(275)
        world_layout.addWidget(self.world_view, 1)
        controls = QHBoxLayout()
        self.legend = QLabel(
            "● Player     ● In-zone Monster     ● Rejected/Outside     ○ Current Target")
        self.legend.setStyleSheet("color:#9bb0bf")
        controls.addWidget(self.legend, 1)
        fit_button = QPushButton("⛶  Fit Zone")
        self.follow_button = QPushButton("♟  Follow Player")
        self.follow_button.setCheckable(True)
        zoom_in = QPushButton("+")
        zoom_out = QPushButton("−")
        zoom_in.setFixedWidth(36)
        zoom_out.setFixedWidth(36)
        fit_button.clicked.connect(self.world_view.fit_zone)
        self.follow_button.toggled.connect(self.world_view.set_follow_player)
        zoom_in.clicked.connect(self.world_view.zoom_in)
        zoom_out.clicked.connect(self.world_view.zoom_out)
        controls.addWidget(fit_button)
        controls.addWidget(self.follow_button)
        controls.addWidget(zoom_in)
        controls.addWidget(zoom_out)
        world_layout.addLayout(controls)
        body.addWidget(world_panel, 1)

        right = panel()
        right.setFixedWidth(230)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 8, 10, 8)
        right_layout.setSpacing(3)
        right_layout.addWidget(section("ZONE & TARGET"))
        self.zone_status = QLabel("NO ZONE")
        self.zone_status.setObjectName("pillGreen")
        self.zone_status.setWordWrap(True)
        self.zone_status.setMinimumHeight(28)
        self.zone_status.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(self.zone_status)
        self.zone_name = QLineEdit()
        self.zone_name.setPlaceholderText("Zone name")
        right_layout.addWidget(self.zone_name)
        self.record_button = QPushButton("◉  RECORD ZONE")
        self.add_button = QPushButton("◆  ADD POSITION")
        self.undo_button = QPushButton("↶  UNDO")
        self.save_button = QPushButton("▣  FINISH & SAVE")
        self.save_button.setObjectName("save")
        self.clear_button = QPushButton("▥  CLEAR ZONE")
        self.clear_button.setObjectName("danger")
        for button in (self.record_button, self.add_button, self.undo_button,
                       self.save_button, self.clear_button):
            right_layout.addWidget(button)
        self.recorder_status = QLabel("")
        self.recorder_status.setObjectName("red")
        self.recorder_status.setWordWrap(True)
        right_layout.addWidget(self.recorder_status)
        self.record_button.clicked.connect(self.record_requested)
        self.add_button.clicked.connect(self.add_position_requested)
        self.undo_button.clicked.connect(self.undo_requested)
        self.save_button.clicked.connect(self.save_requested)
        self.clear_button.clicked.connect(self.clear_requested)
        self.zone_detail = QLabel("Points: 0\nSafety Margin: —\nAuto Return: ON")
        self.zone_detail.setStyleSheet("line-height:1.5")
        right_layout.addWidget(self.zone_detail)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#21455d")
        right_layout.addWidget(line)
        target_group = QGroupBox("CURRENT TARGET")
        target_layout = QVBoxLayout(target_group)
        self.current_target = QLabel("None")
        self.current_target.setWordWrap(True)
        target_layout.addWidget(self.current_target)
        right_layout.addWidget(target_group)
        right_layout.addStretch(1)
        body.addWidget(right)
        root.addLayout(body, 1)

    def apply_snapshot(self, snapshot):
        super().apply_snapshot(snapshot)
        self.world_view.update_snapshot(snapshot)
        self.state_card.value.setText(snapshot.bot_state.upper())
        if snapshot.player_valid and snapshot.player_fresh and snapshot.player is not None:
            self.player_card.value.setText(f"X {snapshot.player[0]:.1f}   Z {snapshot.player[1]:.1f}")
        else:
            self.player_card.value.setText("POSITION UNAVAILABLE")
        if not snapshot.player_valid or not snapshot.player_fresh:
            self.target_card.value.setText("DIST —")
            self.current_target.setText("Distance: —")
        elif snapshot.target:
            self.target_card.value.setText(snapshot.target.name or f"#{snapshot.target.entity_id}")
            distance = "—" if snapshot.target.distance is None else f"{snapshot.target.distance:.1f}"
            self.current_target.setText(
                f"<b style='color:#66e234'>{snapshot.target.name or 'Monster'}</b><br>"
                f"Distance: <span style='color:#16d9f4'>{distance}</span><br>"
                f"Status: {'Valid in zone' if snapshot.target.valid_monster else 'Rejected/outside'}")
        else:
            self.target_card.value.setText("NONE")
            self.current_target.setText("None")
        if snapshot.zone.valid:
            valid = len(snapshot.monsters_in_zone)
            self.zone_card.value.setText(
                f"{valid} MONSTER{'S' if valid != 1 else ''}")
        else:
            self.zone_card.value.setText("—")
        zone_text = {
            ZoneDisplayState.NO_SAVED_ZONE: "NO SAVED ZONE",
            ZoneDisplayState.LOADED_DISCONNECTED:
                "ZONE LOADED, GAME DISCONNECTED",
            ZoneDisplayState.ACTIVE: "ZONE ACTIVE",
            ZoneDisplayState.INVALID: "INVALID SAVED ZONE",
        }[snapshot.zone_display_state]
        self.zone_status.setText(zone_text)
        self.zone_status.setToolTip(zone_text)
        if snapshot.zone.name and not self.zone_name.hasFocus():
            self.zone_name.setText(snapshot.zone.name)
        self.zone_detail.setText(
            f"Points: {len(snapshot.zone.points)}\n"
            f"Safety Margin: {snapshot.zone.safety_margin:g}\n"
            f"Auto Return: {'ON' if snapshot.zone.auto_return else 'OFF'}")

    def set_draft(self, points, recording):
        self.world_view.set_draft(points)
        self.record_button.setText("●  RECORDING" if recording else "◉  RECORD ZONE")
        self.record_button.setChecked(recording)
        if recording or points:
            self.zone_detail.setText(f"Draft Points: {len(points)}\nAdd current player position\nFinish requires 3+ points")

    def set_recording_availability(self, available, recording, has_points,
                                   finish_ready=False, message=""):
        self.record_button.setEnabled(bool(available and not recording))
        self.add_button.setEnabled(bool(available and recording))
        self.undo_button.setEnabled(bool(has_points))
        self.save_button.setEnabled(bool(recording and finish_ready))
        self.clear_button.setEnabled(bool(recording or has_points))
        self.recorder_status.setText(str(message))


class TargetingPage(SnapshotPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(section("TARGETING — ACQUISITION, CLASSIFICATION, SELECTION"))
        grid = QGridLayout()
        self.source = QLabel("—")
        self.scanner = QLabel("—")
        self.classification = QLabel("—")
        self.selection = QLabel("—")
        rows = (("Active source", self.source), ("Scanner", self.scanner),
                ("Classification", self.classification), ("Selection", self.selection))
        for row, (title, value) in enumerate(rows):
            box = panel()
            form = QVBoxLayout(box)
            form.addWidget(QLabel(title))
            value.setObjectName("cyan" if row < 2 else "green")
            value.setWordWrap(True)
            form.addWidget(value)
            grid.addWidget(box, row // 2, row % 2)
        layout.addLayout(grid)
        explanation = QGroupBox("Target pipeline")
        explain_layout = QVBoxLayout(explanation)
        text = QLabel("1. Acquire cached memory units or minimap dots.\n"
                      "2. Reject pets, pooled/dead/unrendered/no-ID/cloaked units.\n"
                      "3. Apply area admission and stable ObjectId identity.\n"
                      "4. Retain the held target unless another is clearly nearer.\n"
                      "5. Route, orbit, time out, or temporarily blacklist safely.")
        text.setWordWrap(True)
        explain_layout.addWidget(text)
        layout.addWidget(explanation)
        layout.addStretch(1)

    def apply_snapshot(self, snapshot):
        super().apply_snapshot(snapshot)
        self.source.setText(snapshot.source.upper())
        memory = snapshot.raw.get("dashboard", {}).get("memory", {})
        self.scanner.setText(str(memory.get("scanner", "Not available")))
        valid = sum(item.valid for item in snapshot.entities if item.kind == "monster")
        ignored = sum(item.ignored for item in snapshot.entities)
        self.classification.setText(f"{valid} valid monsters; {ignored} ignored/rejected")
        self.selection.setText(snapshot.target.name if snapshot.target else "No held target")


class FarmingZonePage(SnapshotPage):
    record_requested = Signal()
    add_position_requested = Signal()
    undo_requested = Signal()
    save_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        controls = panel()
        controls.setFixedWidth(280)
        form = QVBoxLayout(controls)
        form.addWidget(section("FARMING ZONE RECORDER"))
        self.zone_name = QLineEdit()
        self.zone_name.setPlaceholderText("New polygon name")
        form.addWidget(QLabel("Zone name"))
        form.addWidget(self.zone_name)
        self.record_button = QPushButton("Start New Polygon")
        self.add_button = QPushButton("Add Current Position")
        self.undo_button = QPushButton("Undo Last Point")
        self.save_button = QPushButton("Finish & Save")
        self.save_button.setObjectName("save")
        self.clear_button = QPushButton("Clear Draft")
        self.clear_button.setObjectName("danger")
        for button in (self.record_button, self.add_button, self.undo_button,
                       self.save_button, self.clear_button):
            form.addWidget(button)
        self.recorder_status = QLabel("")
        self.recorder_status.setObjectName("red")
        self.recorder_status.setWordWrap(True)
        form.addWidget(self.recorder_status)
        self.record_button.clicked.connect(self.record_requested)
        self.add_button.clicked.connect(self.add_position_requested)
        self.undo_button.clicked.connect(self.undo_requested)
        self.save_button.clicked.connect(self.save_requested)
        self.clear_button.clicked.connect(self.clear_requested)
        self.details = QLabel("No active draft")
        self.details.setWordWrap(True)
        form.addWidget(self.details)
        form.addStretch(1)
        layout.addWidget(controls)
        self.world_view = WorldView()
        layout.addWidget(self.world_view, 1)

    def apply_snapshot(self, snapshot):
        super().apply_snapshot(snapshot)
        self.world_view.update_snapshot(snapshot)
        self.details.setText(
            f"Active area: {snapshot.zone.name or 'none'}\n"
            f"Safety margin: {snapshot.zone.safety_margin if snapshot.zone else 0.0:.1f}\n"
            f"Player: {snapshot.player or 'unavailable'}\n"
            "Recorder uses cached player coordinates from the bot worker.")

    def set_draft(self, points, recording):
        self.world_view.set_draft(points)
        self.details.setText(
            f"Recording: {'YES' if recording else 'NO'}\nPoints: {len(points)}\n"
            "Add Position samples the latest immutable player snapshot.")

    def set_recording_availability(self, available, recording, has_points,
                                   finish_ready=False, message=""):
        self.record_button.setEnabled(bool(available and not recording))
        self.add_button.setEnabled(bool(available and recording))
        self.undo_button.setEnabled(bool(has_points))
        self.save_button.setEnabled(bool(recording and finish_ready))
        self.clear_button.setEnabled(bool(recording or has_points))
        self.recorder_status.setText(str(message))


class CombatPage(SnapshotPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addWidget(section("COMBAT & CONTROL MONITOR"))
        cards = QHBoxLayout()
        self.attack = StatCard("Attack", "⚔", "red")
        self.stick = StatCard("Movement", "✥", "cyan")
        self.action = StatCard("Last action", "◆", "amber")
        for item in (self.attack, self.stick, self.action):
            cards.addWidget(item)
        layout.addLayout(cards)
        safety = QGroupBox("Safety contract")
        text = QLabel("Pause, Stop, Emergency Stop, worker failure, process loss, invalid memory, "
                      "and window close all request neutral stick and released buttons. The original "
                      "terminal loop remains the sole controller owner and performs pad.close() in its finally block.")
        text.setWordWrap(True)
        box = QVBoxLayout(safety)
        box.addWidget(text)
        layout.addWidget(safety)
        layout.addStretch(1)

    def apply_snapshot(self, snapshot):
        super().apply_snapshot(snapshot)
        control = snapshot.raw.get("control", {})
        self.attack.value.setText("HELD" if control.get("attack") else "RELEASED")
        stick = control.get("stick", (0.0, 0.0))
        self.stick.value.setText(f"X {stick[0]:+.2f}  Y {stick[1]:+.2f}")
        dashboard = snapshot.raw.get("dashboard", {})
        self.action.value.setText(str(dashboard.get("action") or "NONE").upper())


class SettingsPage(SnapshotPage):
    save_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        settings_box = panel()
        form_wrap = QVBoxLayout(settings_box)
        form_wrap.addWidget(section("UI & RUNTIME SETTINGS"))
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItems(("memory", "minimap"))
        self.area = QComboBox()
        self.area.setEditable(True)
        self.reconnect = QCheckBox("Allow the unchanged bot's reconnect flow")
        self.follow = QCheckBox("Follow player in world view")
        self.trail = QSpinBox()
        self.trail.setRange(0, 5000)
        self.entities = QSpinBox()
        self.entities.setRange(10, 2000)
        self.log_level = QComboBox()
        self.log_level.addItems(("DEBUG", "INFO", "WARNING", "ERROR"))
        form.addRow("Target source", self.mode)
        form.addRow("Farming area", self.area)
        form.addRow("Reconnect", self.reconnect)
        form.addRow("World view", self.follow)
        form.addRow("Trail points", self.trail)
        form.addRow("Entity cap", self.entities)
        form.addRow("Log level", self.log_level)
        form_wrap.addLayout(form)
        save = QPushButton("Save UI Settings")
        save.setObjectName("save")
        save.clicked.connect(self.save_requested)
        form_wrap.addWidget(save)
        self.notice = QLabel("Changes apply on the next worker start. Existing terminal bot files are never rewritten.")
        self.notice.setWordWrap(True)
        self.notice.setObjectName("muted")
        form_wrap.addWidget(self.notice)
        form_wrap.addStretch(1)
        layout.addWidget(settings_box, 1)

        policy_box = panel()
        policy_layout = QVBoxLayout(policy_box)
        policy_layout.addWidget(section("FAIL-SAFE POLICY COVERAGE"))
        self.policy_table = QTableWidget(0, 2)
        self.policy_table.setHorizontalHeaderLabels(("Failure", "Safe result"))
        self.policy_table.verticalHeader().setVisible(False)
        self.policy_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.policy_table.setSelectionMode(QTableWidget.NoSelection)
        self.policy_table.horizontalHeader().setStretchLastSection(True)
        for code in FailureCode:
            policy = FAILURE_POLICIES[code]
            row = self.policy_table.rowCount()
            self.policy_table.insertRow(row)
            self.policy_table.setItem(row, 0, QTableWidgetItem(code.value.replace("_", " ")))
            self.policy_table.setItem(row, 1, QTableWidgetItem(policy.safe_state.value))
        self.policy_table.resizeColumnsToContents()
        policy_layout.addWidget(self.policy_table)
        layout.addWidget(policy_box, 1)

    def load_settings(self, settings: UiSettings, areas=()):
        self.mode.setCurrentText(settings.mode)
        self.area.clear()
        self.area.addItem("")
        self.area.addItems(list(areas))
        self.area.setCurrentText(settings.selected_area)
        self.reconnect.setChecked(settings.auto_reconnect)
        self.follow.setChecked(settings.follow_player)
        self.trail.setValue(settings.trail_length)
        self.entities.setValue(settings.max_entities)
        self.log_level.setCurrentText(settings.log_level)

    def settings(self, demo_mode=False):
        return UiSettings(mode=self.mode.currentText(),
                          selected_area=self.area.currentText().strip(),
                          auto_reconnect=self.reconnect.isChecked(),
                          follow_player=self.follow.isChecked(),
                          trail_length=self.trail.value(),
                          max_entities=self.entities.value(),
                          log_level=self.log_level.currentText(),
                          demo_mode=demo_mode).validated()

    def apply_snapshot(self, snapshot):
        super().apply_snapshot(snapshot)
        self.notice.setText(
            f"Worker: {snapshot.state.value} | Source: {snapshot.source.upper()} | "
            "Setting changes apply on next start.")
