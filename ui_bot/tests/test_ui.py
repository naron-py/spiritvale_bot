import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui_bot.config import ConfigError
from ui_bot.main_window import MainWindow
from ui_bot.runtime import DemoEngine, DemoRuntime
from ui_bot.widgets.world_view import WorldView


_APP = QApplication.instance() or QApplication([])


class WorldViewTests(unittest.TestCase):
    def test_draws_cached_snapshot_layers_and_supports_view_controls(self):
        view = WorldView()
        view.resize(700, 360)
        snapshot = DemoEngine().next_snapshot(True)

        view.update_snapshot(snapshot)
        _APP.processEvents()

        self.assertGreater(len(view.scene().items()), 10)
        self.assertEqual(view.last_sequence, snapshot.sequence)
        view.zoom_in()
        view.zoom_out()
        view.fit_zone()
        view.set_follow_player(True)
        self.assertTrue(view.follow_player)
        view.set_follow_player(False)
        self.assertFalse(view.follow_player)
        view.close()

    def test_auto_fit_runs_after_the_view_receives_its_final_layout(self):
        view = WorldView()
        view.resize(700, 360)
        view.show()
        view.update_snapshot(DemoEngine().next_snapshot(True))
        QTest.qWait(20)

        self.assertGreater(view.transform().m11(), 2.0)
        view.close()

    def test_rejects_out_of_order_snapshot(self):
        view = WorldView()
        engine = DemoEngine()
        newer = engine.next_snapshot(True)
        older = type(newer).from_mapping({
            "sequence": 0, "timestamp": 0, "state": "PAUSED"})
        view.update_snapshot(newer)
        count = len(view.scene().items())
        view.update_snapshot(older)
        self.assertEqual(view.last_sequence, newer.sequence)
        self.assertEqual(len(view.scene().items()), count)
        view.close()


class MainWindowTests(unittest.TestCase):
    def make_window(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "areas.json").write_text('{"cell":3,"areas":{}}', encoding="utf-8")
        runtime = DemoRuntime()
        window = MainWindow(root, runtime=runtime, demo_mode=True)
        window._test_directory = directory
        return window, runtime

    def test_required_pages_and_safety_controls_are_visible(self):
        window, _ = self.make_window()
        window.show()
        _APP.processEvents()

        self.assertEqual(set(window.nav_buttons),
                         {"Dashboard", "Targeting", "Farming Zone", "Combat", "Settings"})
        self.assertTrue(window.start_button.isVisible())
        self.assertTrue(window.pause_button.isVisible())
        self.assertTrue(window.stop_button.isVisible())
        self.assertTrue(window.emergency_button.isVisible())
        self.assertGreaterEqual(window.minimumWidth(), 1000)
        self.assertGreaterEqual(window.minimumHeight(), 600)
        window.close()

    def test_default_follow_mode_keeps_a_fitted_world_scale(self):
        window, _ = self.make_window()
        window.show()
        QTest.mouseClick(window.start_button, Qt.LeftButton)
        QTest.qWait(100)

        self.assertGreater(window.pages["Dashboard"].world_view.transform().m11(),
                           2.0)
        window.emergency_stop("test complete")
        window.close()

    def test_start_pause_stop_and_emergency_are_idempotent(self):
        window, runtime = self.make_window()
        window.show()
        QTest.mouseClick(window.start_button, Qt.LeftButton)
        QTest.qWait(90)
        self.assertTrue(runtime.started)
        QTest.mouseClick(window.start_button, Qt.LeftButton)
        self.assertTrue(runtime.started)
        QTest.mouseClick(window.pause_button, Qt.LeftButton)
        self.assertFalse(runtime.running)
        QTest.mouseClick(window.pause_button, Qt.LeftButton)
        self.assertFalse(runtime.running)
        QTest.mouseClick(window.start_button, Qt.LeftButton)
        self.assertTrue(runtime.running)
        QTest.mouseClick(window.emergency_button, Qt.LeftButton)
        self.assertFalse(runtime.started)
        self.assertTrue(window.controller.emergency_latched)
        self.assertFalse(window.start_button.isEnabled())
        window.reset_emergency()
        self.assertFalse(window.controller.emergency_latched)
        self.assertTrue(runtime.started)
        self.assertFalse(runtime.running)
        QTest.mouseClick(window.stop_button, Qt.LeftButton)
        window.close()

    def test_only_visible_page_receives_snapshot_updates(self):
        window, _ = self.make_window()
        snapshot = DemoEngine().next_snapshot(True)
        window.show_page("Combat")
        before = {name: page.update_count for name, page in window.pages.items()}
        window.apply_snapshot(snapshot)

        self.assertEqual(window.pages["Combat"].update_count,
                         before["Combat"] + 1)
        for name in ("Targeting", "Farming Zone", "Settings"):
            self.assertEqual(window.pages[name].update_count, before[name])
        window.close()

    def test_activity_log_is_bounded(self):
        window, _ = self.make_window()
        for index in range(1500):
            window.append_log(f"line {index}")
        self.assertLessEqual(window.activity_log.document().blockCount(), 1000)
        window.close()

    def test_buff_slots_add_remove_and_reset_without_restarting_runtime(self):
        window, runtime = self.make_window()
        page = window.pages["Settings"]
        original_ids = [row.slot_id for row in page.buff_rows]
        engine = runtime.engine

        row = page.add_buff_slot()
        self.assertFalse(row.enabled.isChecked())
        self.assertNotIn(row.slot_id, original_ids)
        self.assertEqual(len(page.buff_rows), 7)
        self.assertIs(runtime.engine, engine)
        self.assertFalse(runtime.running)

        second = page.add_buff_slot()
        page.remove_buff_slot(row.slot_id)
        replacement = page.add_buff_slot()
        self.assertNotEqual(replacement.slot_name, second.slot_name)
        page.remove_buff_slot(second.slot_id)
        page.remove_buff_slot(replacement.slot_id)
        self.assertEqual([item.slot_id for item in page.buff_rows], original_ids)
        page.buff_rows[0].enabled.setChecked(False)
        page.add_buff_slot()
        page.reset_controller_defaults()

        self.assertEqual([item.slot_id for item in page.buff_rows], original_ids)
        self.assertTrue(all(item.enabled.isChecked() for item in page.buff_rows))
        self.assertIs(runtime.engine, engine)
        self.assertFalse(runtime.running)
        window.close()

    def test_execution_preview_updates_from_current_slot_values(self):
        window, _ = self.make_window()
        page = window.pages["Settings"]

        page.buff_rows[2].enabled.setChecked(False)
        page.buff_rows[3].button.setCurrentData("y")
        page.attack_rows[1].enabled.setChecked(False)
        _APP.processEvents()

        preview = page.preview.text()
        self.assertIn("[D-Pad Up]", preview)
        self.assertIn("[Y]", preview)
        self.assertIn("[D-Pad Left] — disabled", preview)
        self.assertIn("[LB] — continuous", preview)
        self.assertNotIn("[RB] +", preview)
        window.close()

    def test_settings_controller_panel_does_not_need_horizontal_scrolling(self):
        window, _runtime = self.make_window()
        page = window.pages["Settings"]
        window.resize(1280, 760)
        window.show_page("Settings")
        window.show()
        _APP.processEvents()

        self.assertEqual(page.controls_scroll.horizontalScrollBar().maximum(), 0)
        window.close()

    def test_invalid_active_binding_names_conflicting_slots(self):
        window, _ = self.make_window()
        page = window.pages["Settings"]
        page.buff_rows[0].button.setCurrentData("lb")

        with self.assertRaisesRegex(ConfigError, "Buff Slot 1.*Attack Skill 1"):
            page.settings()

        window.close()

    def test_save_applies_controller_slots_without_restarting_running_worker(self):
        window, runtime = self.make_window()
        page = window.pages["Settings"]
        QTest.mouseClick(window.start_button, Qt.LeftButton)
        engine = runtime.engine
        page.buff_rows[2].enabled.setChecked(False)

        window.save_settings()

        self.assertIs(runtime.engine, engine)
        self.assertTrue(runtime.running)
        self.assertFalse(runtime.control_config["buff_slots"][2]["enabled"])
        window.emergency_stop("test complete")
        window.close()

    def test_invalid_save_keeps_last_file_and_does_not_emergency_stop(self):
        window, _ = self.make_window()
        window.save_settings()
        original = window.config_store.path.read_bytes()
        page = window.pages["Settings"]
        page.buff_rows[0].button.setCurrentData("lb")

        window.save_settings()

        self.assertEqual(window.config_store.path.read_bytes(), original)
        self.assertFalse(window.controller.emergency_latched)
        self.assertIn("Buff Slot 1", page.validation.text())
        window.close()


if __name__ == "__main__":
    unittest.main()
