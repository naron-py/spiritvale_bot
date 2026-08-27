"""Top-level SpiritVale Farm Bot Qt window."""

from __future__ import annotations

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from .app_controller import AppController
from .config import AtomicConfigStore, ConfigError, UiSettings
from .model import (AutomationState, BotSnapshot, BotState, ConnectionState,
                    FailureCode, ZoneDisplayState, ZoneRecordingState,
                    ZoneSnapshot)
from .pages import (CombatPage, DashboardPage, FarmingZonePage, SettingsPage,
                    TargetingPage)
from .readiness import evaluate_start_readiness, normalize_mode
from .theme import STYLESHEET
from .zone_editor import ZoneDraft, ZoneError, ZoneStore


class MainWindow(QMainWindow):
    PAGE_NAMES = ("Dashboard", "Targeting", "Farming Zone", "Combat", "Settings")

    def __init__(self, project_root: str | Path, runtime,
                 demo_mode: bool = False, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root).resolve()
        self.runtime = runtime
        self.demo_mode = demo_mode
        self._closing = False
        self._recording = False
        self._zone_revision = 0
        self.selected_mode = "waiting"
        self._last_memory_ready = False
        self.zone_recording_state = ZoneRecordingState.INACTIVE
        self.recorder_message = (
            "Cannot add point: valid player position unavailable.")
        self.draft = ZoneDraft("")
        self.latest_snapshot = BotSnapshot.safe(
            BotState.DISCONNECTED, "Waiting for game…")
        self.config_store = AtomicConfigStore(
            self.project_root / "ui_bot" / "state" / "settings.json")
        self.zone_store = ZoneStore(self.project_root / "areas.json")
        self.logger = self._make_logger()
        self.settings_value = self.config_store.load()
        if demo_mode:
            self.settings_value = UiSettings(**{
                **self.settings_value.__dict__, "demo_mode": True})
        self.saved_zone = ZoneSnapshot()
        self.zone_load_error = ""
        self._load_saved_zone()
        self.controller = AppController(runtime, self.append_log)
        self.recovery_timer = QTimer(self)
        self.recovery_timer.setInterval(1000)
        self.recovery_timer.timeout.connect(self._recovery_tick)
        self.recovery_timer.start()

        self.setWindowTitle("SpiritVale Farm Bot")
        self.setMinimumSize(1060, 650)
        self.resize(1280, 760)
        self.setStyleSheet(STYLESHEET)
        self._build_ui()
        self._connect_runtime()
        self._connect_zone_controls()
        self._load_settings()
        self.apply_snapshot(self.latest_snapshot)
        self._attach_monitor()
        self.show_page("Dashboard")
        if self.config_store.last_warning:
            self.append_log("[Config] " + self.config_store.last_warning)
        if demo_mode:
            self.demo_badge.setText("● DEMO MODE — NO GAME INPUT")
            self.demo_badge.show()
        self._refresh_controls()

    def _make_logger(self):
        log_dir = self.project_root / "ui_bot" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"spiritvale.ui.{id(self)}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = RotatingFileHandler(log_dir / "ui.log", maxBytes=1_000_000,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        return logger

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(48)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(12, 5, 12, 5)
        logo = QLabel("◎")
        logo.setStyleSheet("font-size:27px;color:#16d9f4")
        title = QLabel("FARM BOT")
        title.setObjectName("title")
        self.connection_badge = QLabel("● GAME DISCONNECTED")
        self.connection_badge.setObjectName("pillGreen")
        self.memory_badge = QLabel("⌁ MEMORY IDLE")
        self.memory_badge.setObjectName("pillCyan")
        self.mode_badge = QLabel("MODE: WAITING")
        self.mode_badge.setObjectName("pillCyan")
        self.mode_reason = QLabel("Game is not connected.")
        self.mode_reason.setObjectName("muted")
        self.mode_reason.setFixedHeight(25)
        self.mode_reason.setStyleSheet(
            "padding:3px 12px;background:#071724;border-bottom:1px solid #17384d")
        self.demo_badge = QLabel("")
        self.demo_badge.setObjectName("amber")
        self.demo_badge.hide()
        top.addWidget(logo)
        top.addWidget(title)
        top.addSpacing(24)
        top.addWidget(self.connection_badge)
        top.addWidget(self.memory_badge)
        top.addWidget(self.mode_badge)
        top.addWidget(self.demo_badge)
        top.addStretch(1)
        self.start_button = QPushButton("▶  START")
        self.start_button.setObjectName("start")
        self.pause_button = QPushButton("Ⅱ  PAUSE")
        self.pause_button.setObjectName("pause")
        self.stop_button = QPushButton("■  STOP")
        self.stop_button.setObjectName("stop")
        self.emergency_button = QPushButton("!  EMERGENCY STOP")
        self.emergency_button.setObjectName("emergency")
        for button in (self.start_button, self.pause_button, self.stop_button,
                       self.emergency_button):
            top.addWidget(button)
        outer.addWidget(topbar)
        outer.addWidget(self.mode_reason)

        middle = QHBoxLayout()
        middle.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(150)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(5, 9, 5, 9)
        nav.setSpacing(3)
        icons = ("⌂", "◎", "⬡", "⚔", "⚙")
        self.nav_buttons = {}
        for name, icon in zip(self.PAGE_NAMES, icons):
            button = QPushButton(f"{icon}   {name}")
            button.setObjectName("nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, n=name: self.show_page(n))
            nav.addWidget(button)
            self.nav_buttons[name] = button
        nav.addStretch(1)
        self.version_label = QLabel("UI v1.0\nRead-only memory adapter")
        self.version_label.setObjectName("muted")
        self.version_label.setAlignment(Qt.AlignCenter)
        nav.addWidget(self.version_label)
        middle.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(10, 8, 8, 8)
        content_layout.setSpacing(7)
        self.stack = QStackedWidget()
        self.pages = {
            "Dashboard": DashboardPage(),
            "Targeting": TargetingPage(),
            "Farming Zone": FarmingZonePage(),
            "Combat": CombatPage(),
            "Settings": SettingsPage(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        content_layout.addWidget(self.stack, 1)

        log_frame = QFrame()
        log_frame.setObjectName("panel")
        log_frame.setFixedHeight(118)
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(9, 5, 9, 6)
        log_title = QHBoxLayout()
        caption = QLabel("ACTIVITY LOG")
        caption.setObjectName("section")
        self.rate_label = QLabel("UI 15 FPS    │    BOT 20 Hz")
        self.rate_label.setObjectName("cyan")
        log_title.addWidget(caption)
        log_title.addStretch(1)
        log_title.addWidget(self.rate_label)
        log_layout.addLayout(log_title)
        self.activity_log = QPlainTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setMaximumBlockCount(1000)
        self.activity_log.setFrameShape(QFrame.NoFrame)
        log_layout.addWidget(self.activity_log)
        content_layout.addWidget(log_frame)
        middle.addWidget(content, 1)
        outer.addLayout(middle, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        footer.setFixedHeight(56)
        flow = QHBoxLayout(footer)
        flow.setContentsMargins(55, 6, 55, 6)
        steps = ("CONNECT GAME", "RECORD ZONE", "CONFIGURE", "START BOT", "MONITOR")
        self.step_labels = []
        for index, text in enumerate(steps, 1):
            badge = QLabel(str(index))
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedSize(24, 24)
            badge.setStyleSheet("background:#123752;border:1px solid #38a9e6;border-radius:12px;color:white;font-weight:700")
            label = QLabel(text)
            label.setStyleSheet("font-weight:600;color:#bdcbd5")
            flow.addWidget(badge)
            flow.addWidget(label)
            self.step_labels.append((badge, label))
            if index != len(steps):
                arrow = QLabel("⟶")
                arrow.setAlignment(Qt.AlignCenter)
                arrow.setStyleSheet("font-size:20px;color:#718594")
                flow.addWidget(arrow, 1)
        outer.addWidget(footer)

        self.start_button.clicked.connect(self.start_bot)
        self.pause_button.clicked.connect(self.pause_bot)
        self.stop_button.clicked.connect(self.stop_bot)
        self.emergency_button.clicked.connect(self._emergency_clicked)
        self.pages["Settings"].save_requested.connect(self.save_settings)
        shortcut = QShortcut(QKeySequence("Ctrl+Shift+F12"), self)
        shortcut.activated.connect(lambda: self.emergency_stop("Emergency hotkey"))
        self.emergency_shortcut = shortcut
        self.end_shortcut = QShortcut(QKeySequence("End"), self)
        self.end_shortcut.setContext(Qt.ApplicationShortcut)
        self.end_shortcut.activated.connect(self.end_hotkey)

    def _connect_runtime(self):
        signals = self.runtime.signals
        signals.snapshot.connect(self._snapshot_received)
        signals.event.connect(self.append_log)
        signals.failure.connect(self._runtime_failure)
        if hasattr(signals, "worker_finished"):
            signals.worker_finished.connect(self._runtime_finished)
            signals.monitor_status.connect(self._monitor_status)
        else:
            signals.exited.connect(self._runtime_exited)

    def _connect_zone_controls(self):
        for page in (self.pages["Dashboard"], self.pages["Farming Zone"]):
            page.record_requested.connect(self.start_recording)
            page.add_position_requested.connect(self.add_position)
            page.undo_requested.connect(self.undo_position)
            page.save_requested.connect(self.finish_zone)
            page.clear_requested.connect(self.clear_zone)

    def _area_names(self):
        try:
            return self.zone_store.names()
        except ZoneError as exc:
            self.append_log(f"[Zone] {exc}")
            return []

    def _load_saved_zone(self):
        selected = self.settings_value.selected_area
        try:
            zone = self.zone_store.load_selected(selected)
        except ZoneError as exc:
            self.saved_zone = ZoneSnapshot(name=selected)
            self.zone_load_error = str(exc)
            self.latest_snapshot = self.latest_snapshot.with_zone(
                self.saved_zone, ZoneDisplayState.INVALID)
            return
        self.saved_zone = zone
        if zone.valid and selected != zone.name:
            self.settings_value = UiSettings(**{
                **self.settings_value.__dict__, "selected_area": zone.name})
        state = (ZoneDisplayState.LOADED_DISCONNECTED if zone.valid else
                 ZoneDisplayState.NO_SAVED_ZONE)
        self.latest_snapshot = self.latest_snapshot.with_zone(zone, state)

    def _saved_zone_overlay(self, snapshot):
        if snapshot.zone.valid:
            state = (ZoneDisplayState.ACTIVE
                     if snapshot.connection_state == ConnectionState.CONNECTED
                     else ZoneDisplayState.LOADED_DISCONNECTED)
            return snapshot.with_zone(snapshot.zone, state)
        if self.saved_zone.valid:
            state = (ZoneDisplayState.ACTIVE
                     if snapshot.connection_state == ConnectionState.CONNECTED
                     else ZoneDisplayState.LOADED_DISCONNECTED)
            return snapshot.with_zone(self.saved_zone, state)
        if self.zone_load_error:
            return snapshot.with_zone(self.saved_zone, ZoneDisplayState.INVALID)
        return snapshot.with_zone(ZoneSnapshot(), ZoneDisplayState.NO_SAVED_ZONE)

    def _load_settings(self):
        self.pages["Settings"].load_settings(self.settings_value,
                                              self._area_names())
        self.pages["Dashboard"].zone_name.setText(
            self.settings_value.selected_area)
        self.pages["Farming Zone"].zone_name.setText(
            self.settings_value.selected_area)
        self.pages["Dashboard"].world_view.set_follow_player(
            self.settings_value.follow_player)
        self.pages["Dashboard"].follow_button.setChecked(
            self.settings_value.follow_player)

    def show_page(self, name):
        page = self.pages[name]
        self.stack.setCurrentWidget(page)
        for key, button in self.nav_buttons.items():
            button.setChecked(key == name)
        page.apply_snapshot(self.latest_snapshot)
        if name in ("Dashboard", "Farming Zone"):
            page.set_draft(self.draft.points, self._recording)

    def start_bot(self):
        readiness = self._start_readiness()
        if not readiness.can_start:
            self.append_log("[Mode] Start blocked: " + readiness.reason)
            self._refresh_controls()
            return
        self.selected_mode = readiness.selected_mode
        options = self._runtime_options(readiness.selected_mode)
        if options is None:
            return
        if (self.selected_mode == "pixel"
                and self.pages["Settings"].area.currentText().strip()):
            self.append_log("[Mode] Polygon unavailable in Pixel Mode")
        if self.controller.start(options):
            self.append_log(f"[Mode] Starting {self.selected_mode.upper()} mode.")
        self._refresh_controls()

    def _runtime_options(self, selected_mode=None):
        try:
            settings = self.pages["Settings"].settings(self.demo_mode)
        except ConfigError as exc:
            self.controller.fail(FailureCode.CONFIG_INVALID, exc)
            self._refresh_controls()
            return None
        mode = settings.mode
        if selected_mode is not None:
            mode = ("minimap" if normalize_mode(selected_mode) == "pixel"
                    else "memory")
        area = settings.selected_area if normalize_mode(mode) == "memory" else ""
        return {"mode": mode, "area": area,
                "area_revision": self._zone_revision if area else 0,
                "auto_reconnect": settings.auto_reconnect,
                "trail_length": settings.trail_length,
                "max_entities": settings.max_entities}

    def _attach_monitor(self):
        options = self._runtime_options()
        if options is not None:
            self.controller.attach(options)

    def pause_bot(self):
        self.controller.pause()
        self._refresh_controls()

    def end_hotkey(self):
        """Mirror terminal End through the same START readiness gate."""
        if self.controller.automation_state == AutomationState.RUNNING:
            self.pause_bot()
        else:
            self.start_bot()

    def stop_bot(self):
        if self.controller.state in (BotState.RUNNING, BotState.STARTING):
            if QMessageBox.question(self, "Stop bot",
                                    "Stop the bot and release all controller inputs?",
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) != QMessageBox.Yes:
                return
        self.controller.stop()
        self._refresh_controls()

    def _emergency_clicked(self):
        if self.controller.emergency_latched:
            self.reset_emergency()
        else:
            self.emergency_stop("Emergency Stop button")

    def emergency_stop(self, reason):
        self.controller.emergency_stop(reason)
        self._refresh_controls()

    def reset_emergency(self):
        if self.controller.reset_emergency():
            self.apply_snapshot(self.controller.last_snapshot)
            self._attach_monitor()
        self._refresh_controls()

    def _snapshot_received(self, snapshot):
        if self.controller.accept_snapshot(snapshot):
            self.apply_snapshot(snapshot)
        self._refresh_controls()

    def apply_snapshot(self, snapshot):
        was_available = self._player_position_available()
        snapshot = self._saved_zone_overlay(snapshot)
        self.latest_snapshot = snapshot
        self.connection_badge.setText({
            ConnectionState.CONNECTED: "● GAME CONNECTED",
            ConnectionState.CONNECTING: "● GAME CONNECTING",
            ConnectionState.ERROR: "● GAME ERROR",
            ConnectionState.DISCONNECTED: "● GAME DISCONNECTED",
        }[snapshot.connection_state])
        self.memory_badge.setText(
            "⌁ MEMORY READ ACTIVE" if snapshot.memory_active else
            "⌁ MEMORY READ UNAVAILABLE")
        memory_became_ready = bool(
            snapshot.memory_ready and not self._last_memory_ready)
        self._last_memory_ready = snapshot.memory_ready
        pixel_is_running = bool(
            snapshot.memory_ready
            and self.controller.automation_state == AutomationState.RUNNING
            and self.selected_mode == "pixel"
            and normalize_mode(snapshot.active_mode) == "pixel")
        preferred_mode = normalize_mode(
            self.pages["Settings"].mode.currentText())
        if pixel_is_running and preferred_mode == "memory":
            options = self._runtime_options("memory")
            if options is not None and self.controller.pause():
                self.selected_mode = "memory"
                self.append_log(
                    "[Mode] Memory scan ready; switching Pixel fallback to "
                    "Memory Mode with area enforcement.")
                if self.controller.start(options):
                    self.append_log("[Mode] Starting MEMORY mode.")
        elif (pixel_is_running and memory_became_ready
              and preferred_mode == "pixel"):
            self.append_log(
                "[Mode] Memory Mode is available; Stop and start again to switch.")
        current = self.stack.currentWidget()
        if current is not None:
            current.apply_snapshot(snapshot)
            if current in (self.pages["Dashboard"], self.pages["Farming Zone"]):
                current.set_draft(self.draft.points, self._recording)
        for line in snapshot.logs:
            self.append_log(line)
        if snapshot.error:
            self.append_log("[Attach] " + snapshot.error)
        self._refresh_recorder_controls()
        if self._recording and was_available and not self._player_position_available():
            self.append_log("[Zone] " + self.recorder_message)
        self._update_process_steps(snapshot)

    def _runtime_failure(self, code, error):
        self.controller.fail(code, error)
        self.apply_snapshot(self.controller.last_snapshot)
        self._refresh_controls()

    def _runtime_exited(self, requested, details):
        self.controller.worker_exited(requested, details)
        self.append_log(f"[Runtime] {details}")
        if not requested:
            self.apply_snapshot(self.controller.last_snapshot)
        self._refresh_controls()

    def _runtime_finished(self, result):
        self.controller.worker_finished(result)
        self.append_log(
            f"[Runtime] {result.purpose.value} worker exited "
            f"code={result.exit_code} status={result.status_name}")
        self.apply_snapshot(self.controller.last_snapshot)
        self._refresh_controls()

    def _monitor_status(self, state, message):
        self.controller.monitor_status(state, message)
        self.apply_snapshot(self.controller.last_snapshot)
        self._refresh_controls()

    def _recovery_tick(self):
        before = self.controller.state
        self.controller.tick_recovery()
        if self.controller.state != before:
            self.apply_snapshot(self.controller.last_snapshot)
            self._refresh_controls()

    def _refresh_controls(self):
        automation = self.controller.automation_state
        latched = self.controller.emergency_latched
        switching = self.controller.state == BotState.SWITCHING_MODE
        readiness = self._start_readiness()
        self.start_button.setEnabled(
            not latched and not switching and readiness.can_start and automation in (
                AutomationState.IDLE, AutomationState.PAUSED))
        self.start_button.setToolTip(readiness.reason)
        self.pause_button.setEnabled(
            not latched and not switching
            and automation in (
                AutomationState.RUNNING, AutomationState.RECOVERING))
        self.stop_button.setEnabled(
            not latched and automation in (
                AutomationState.RUNNING, AutomationState.RECOVERING,
                AutomationState.PAUSED)
            or (not latched and switching))
        self.emergency_button.setText(
            "↻  RESET E-STOP" if latched else "!  EMERGENCY STOP")
        self.rate_label.setText(
            f"UI 15 FPS    │    BOT 20 Hz    │    {automation.value}")
        if switching:
            self.mode_badge.setText("MODE: WAITING")
            self.mode_reason.setText(
                f"Switching to {normalize_mode(self.selected_mode).title()} Mode…")
        elif automation == AutomationState.RECOVERING:
            self.mode_badge.setText("MODE: WAITING")
            reason = self.controller.recovery_reason or readiness.reason
            self.mode_reason.setText(f"Recovering automatically: {reason}")
        elif automation == AutomationState.RUNNING:
            mode = normalize_mode(self.latest_snapshot.active_mode)
            if mode == "waiting":
                mode = normalize_mode(self.selected_mode)
            self.mode_badge.setText(f"MODE: {mode.upper()}")
            self.mode_reason.setText(
                "Polygon unavailable in Pixel Mode" if mode == "pixel"
                else "Memory Mode active")
        else:
            self.mode_badge.setText("MODE: WAITING")
            self.mode_reason.setText(readiness.reason)
        self._refresh_recorder_controls()

    def _start_readiness(self):
        snapshot = self.latest_snapshot
        game_connected = bool(
            snapshot.connection_state == ConnectionState.CONNECTED
            and snapshot.connected and snapshot.memory_session_valid)
        memory_ready = bool(
            snapshot.memory_ready and snapshot.memory_active
            and snapshot.player is not None and snapshot.player_valid
            and snapshot.player_fresh)
        memory_reason = snapshot.player_error
        if not snapshot.memory_session_valid:
            memory_reason = "memory session is invalid"
        elif not memory_reason and not memory_ready:
            memory_reason = "fresh player position unavailable"
        return evaluate_start_readiness(
            game_connected, memory_ready, snapshot.pixel_ready,
            self.pages["Settings"].mode.currentText(), memory_reason,
            snapshot.pixel_error)

    def _update_process_steps(self, snapshot):
        completed = 0
        if snapshot.connected:
            completed = 1
        if snapshot.zone.name:
            completed = 2
        if self.settings_value:
            completed = max(completed, 3)
        if snapshot.state in (BotState.STARTING, BotState.RUNNING,
                              BotState.RECOVERING, BotState.PAUSED):
            completed = 4
        if snapshot.sequence > 1:
            completed = 5
        for index, (badge, label) in enumerate(self.step_labels, 1):
            active = index <= completed
            badge.setStyleSheet(
                "background:#0c7140;border:1px solid #44df83;border-radius:12px;color:white;font-weight:700"
                if active else
                "background:#123752;border:1px solid #38a9e6;border-radius:12px;color:white;font-weight:700")

    def append_log(self, message):
        message = str(message).strip()
        if not message:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        self.activity_log.appendPlainText(line)
        self.logger.info(message)

    def save_settings(self):
        try:
            settings = self.pages["Settings"].settings(self.demo_mode)
            self.zone_store.select(settings.selected_area)
            self.config_store.save(settings)
            self.settings_value = settings
            self.saved_zone = self.zone_store.load_selected(
                settings.selected_area)
            self.zone_load_error = ""
            self.append_log("[Config] UI settings saved atomically.")
            self.pages["Dashboard"].world_view.set_follow_player(
                settings.follow_player)
            self.pages["Dashboard"].follow_button.setChecked(
                settings.follow_player)
        except (ConfigError, ZoneError) as exc:
            self.controller.fail(FailureCode.CONFIG_READ_ONLY, exc)
        self._refresh_controls()

    def _zone_name(self):
        active = self.stack.currentWidget()
        if active is self.pages["Farming Zone"]:
            return active.zone_name.text().strip()
        return self.pages["Dashboard"].zone_name.text().strip()

    def start_recording(self):
        if not self._player_position_available():
            self.append_log("[Zone] " + self.recorder_message)
            self._refresh_recorder_controls()
            return
        name = self._zone_name()
        if not name:
            QMessageBox.warning(self, "Zone name required",
                                "Enter a polygon name before recording.")
            return
        if self.draft.points:
            answer = QMessageBox.question(
                self, "Discard current draft?",
                "Starting a new recording clears the unsaved points.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.draft = ZoneDraft(name)
        self._recording = True
        self.zone_recording_state = ZoneRecordingState.RECORDING
        self.append_log(f"[Zone] Recording polygon {name!r}.")
        self._refresh_draft()

    def add_position(self):
        if not self._recording:
            self.append_log("[Zone] Start recording before adding a position.")
            return
        if not self._player_position_available():
            self.append_log("[Zone] " + self.recorder_message)
            self._refresh_recorder_controls()
            return
        try:
            point = self.draft.add(self.latest_snapshot.player)
            self.zone_recording_state = ZoneRecordingState.RECORDING
            self.append_log(f"[Zone] Point {len(self.draft.points)} added at {point[0]:.2f}, {point[1]:.2f}.")
        except ZoneError as exc:
            self.zone_recording_state = ZoneRecordingState.INVALID
            self.append_log(f"[Zone] {exc}")
        self._refresh_draft()

    def undo_position(self):
        point = self.draft.undo()
        self.append_log("[Zone] Last point removed." if point else
                        "[Zone] Draft has no points to undo.")
        self._refresh_draft()

    def finish_zone(self):
        try:
            points = self.draft.validate()
        except ZoneError as exc:
            self.zone_recording_state = ZoneRecordingState.INVALID
            QMessageBox.warning(self, "Invalid polygon", str(exc))
            self.append_log(f"[Zone] Save rejected: {exc}")
            return
        if self.draft.name in self._area_names():
            answer = QMessageBox.question(
                self, "Replace existing zone?",
                f"Replace the saved area {self.draft.name!r}?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        try:
            self.zone_store.save_polygon(self.draft.name, points, select=True)
        except ZoneError as exc:
            self.append_log(f"[Zone] Save failed: {exc}")
            return
        self._zone_revision += 1
        self.settings_value = UiSettings(**{
            **self.settings_value.__dict__, "selected_area": self.draft.name})
        try:
            self.config_store.save(self.settings_value)
        except ConfigError as exc:
            self.append_log(f"[Config] Zone saved, but UI selection settings failed: {exc}")
        self.saved_zone = self.zone_store.load_selected(self.draft.name)
        self.zone_load_error = ""
        self._recording = False
        self.zone_recording_state = ZoneRecordingState.READY
        self.append_log(f"[Zone] Polygon {self.draft.name!r} saved with {len(points)} points.")
        self.pages["Settings"].load_settings(self.settings_value,
                                              self._area_names())
        self.apply_snapshot(self.latest_snapshot.with_zone(
            self.saved_zone,
            ZoneDisplayState.ACTIVE if self.latest_snapshot.connected else
            ZoneDisplayState.LOADED_DISCONNECTED))
        self._refresh_draft()

    def clear_zone(self):
        if not self.draft.points:
            return
        if QMessageBox.question(self, "Clear draft", "Discard every unsaved point?",
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        self.draft.clear()
        self._recording = False
        self.zone_recording_state = ZoneRecordingState.INACTIVE
        self.append_log("[Zone] Draft cleared.")
        self._refresh_draft()

    def _refresh_draft(self):
        for page in (self.pages["Dashboard"], self.pages["Farming Zone"]):
            page.set_draft(self.draft.points, self._recording)
        self._refresh_recorder_controls()

    def _player_position_available(self):
        snapshot = self.latest_snapshot
        return bool(
            snapshot.connection_state == ConnectionState.CONNECTED
            and snapshot.memory_active
            and snapshot.player is not None
            and snapshot.player_valid
            and snapshot.player_fresh
            and snapshot.memory_session_valid
        )

    def _refresh_recorder_controls(self):
        available = self._player_position_available()
        message = "" if available else self.recorder_message
        has_points = bool(self.draft.points)
        finish_ready = len(set(self.draft.points)) >= 3
        for page in (self.pages["Dashboard"], self.pages["Farming Zone"]):
            page.set_recording_availability(
                available, self._recording, has_points, finish_ready, message)

    def closeEvent(self, event: QCloseEvent):
        if self._closing:
            event.accept()
            return
        if self.controller.state in (BotState.STARTING, BotState.RUNNING,
                                     BotState.PAUSED):
            answer = QMessageBox.question(
                self, "Exit Farm Bot",
                "The bot is active. Stop it, release all inputs, and exit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self._closing = True
        self.controller.emergency_stop("Window closing")
        try:
            self.runtime.shutdown()
        except Exception as exc:
            self.logger.exception("runtime shutdown failed: %s", exc)
        for handler in list(self.logger.handlers):
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)
        event.accept()
