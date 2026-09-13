"""Persistent Memory combat spacing and its Settings UI contract."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ui_bot.config import AtomicConfigStore, ConfigError, UiSettings


class CombatSpacingSettingsTests(unittest.TestCase):
    def test_defaults_for_existing_schemas_reach_control_payload(self):
        for raw in ({}, {"schema": 1}, {"schema": 2}):
            with self.subTest(raw=raw):
                settings = UiSettings.from_mapping(raw)
                self.assertEqual(settings.control_config()["combat_spacing"], {
                    "min_distance": 1.8, "resume_distance": 2.5,
                    "max_distance": 3.2})
                self.assertEqual(settings.combat_min_distance, 1.8)
                self.assertEqual(settings.combat_resume_distance, 2.5)
                self.assertEqual(settings.combat_max_distance, 3.2)


    def test_invalid_distances_rejected_before_persistence(self):
        fields = ("combat_min_distance", "combat_resume_distance", "combat_max_distance")
        invalid = (True, False, None, "2.5", [], {}, float("nan"),
                   float("inf"), -float("inf"), 0, -1, 101, 10 ** 400)
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicConfigStore(Path(directory) / "settings.json")
            store.save(UiSettings())
            original = store.path.read_bytes()
            for field in fields:
                for value in invalid:
                    with self.subTest(field=field, value=value):
                        with self.assertRaises(ConfigError):
                            replace(UiSettings(), **{field: value}).validated()
                        with self.assertRaises(ConfigError):
                            UiSettings.from_mapping({field: value})
                        with self.assertRaises(ConfigError):
                            store.save(replace(UiSettings(), **{field: value}))
                        self.assertEqual(store.path.read_bytes(), original)
            for distances in ((2, 2, 3), (1, 3, 3), (3, 2, 4), (1, 4, 3)):
                with self.subTest(distances=distances), self.assertRaises(ConfigError):
                    UiSettings.from_mapping(dict(zip(fields, distances)))

    def test_custom_distances_round_trip_and_recover_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicConfigStore(Path(directory) / "settings.json")
            custom = replace(UiSettings(), combat_min_distance=2,
                             combat_resume_distance=4.25, combat_max_distance=100)
            store.save(custom)
            self.assertEqual(store.load(), custom)
            self.assertEqual(json.loads(store.path.read_text())["combat_resume_distance"], 4.25)
            store.save(UiSettings())
            store.path.write_text('{"combat_min_distance": false}', encoding="utf-8")
            self.assertEqual(store.load(), custom)
            self.assertIn("loaded backup", store.last_warning)


class CombatSpacingUiTests(unittest.TestCase):
    def test_controls_save_live_reload_validate_and_fit_1280x760(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QLabel, QPushButton
        from ui_bot.main_window import MainWindow
        from ui_bot.runtime import DemoRuntime

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "areas.json").write_text('{"cell":3,"areas":{}}', encoding="utf-8")
            runtime = DemoRuntime()
            window = MainWindow(root, runtime=runtime, demo_mode=True)
            try:
                window.resize(1280, 760)
                window.show()
                window.show_page("Settings")
                page = window.pages["Settings"]
                app.processEvents()
                controls = page.findChildren(QDoubleSpinBox)
                self.assertEqual(len(controls), 3)
                expected = {"combat_min_distance": 2.1,
                            "combat_resume_distance": 2.8,
                            "combat_max_distance": 4.2}
                for name, value in expected.items():
                    control = getattr(page, name)
                    self.assertEqual(control.value(), getattr(window.settings_value, name))
                    control.setValue(value)
                labels = {label.text() for label in page.findChildren(QLabel)}
                for label in ("Retreat below", "Preferred distance", "Approach above"):
                    self.assertIn(label, labels)
                help_text = " ".join(labels).lower()
                for text in ("memory-only", "world units", "hysteresis", "apply live"):
                    self.assertIn(text, help_text)
                save = page.findChild(QPushButton, "save")
                QTest.mouseClick(save, Qt.LeftButton)
                saved = window.config_store.load()
                for name, value in expected.items():
                    self.assertEqual(getattr(saved, name), value)
                self.assertEqual(runtime.control_config["combat_spacing"], {
                    "min_distance": 2.1, "resume_distance": 2.8, "max_distance": 4.2})
                page.load_settings(UiSettings())
                page.load_settings(saved)
                for name, value in expected.items():
                    self.assertEqual(getattr(page, name).value(), value)
                page.combat_min_distance.setValue(3.0)
                QTest.mouseClick(save, Qt.LeftButton)
                self.assertIn("combat distances", page.validation.text())
                self.assertEqual(window.config_store.load(), saved)
                self.assertEqual(runtime.control_config, saved.control_config())
                row = page.add_buff_slot()
                app.processEvents()
                self.assertEqual((window.width(), window.height()), (1280, 760))
                self.assertEqual(page.controls_scroll.horizontalScrollBar().maximum(), 0)
                self.assertTrue(row.remove_button.isVisible())
                for widget in (*controls, save, page.validation):
                    self.assertTrue(widget.isVisible())
                    top_left = widget.mapTo(window, QPoint(0, 0))
                    self.assertTrue(window.rect().contains(top_left))
                    self.assertTrue(window.rect().contains(
                        widget.mapTo(window, widget.rect().bottomRight())))
            finally:
                window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
