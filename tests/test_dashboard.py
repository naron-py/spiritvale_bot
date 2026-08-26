import threading
import unittest

from minimap_bot import dashboard_snapshot, dashboard_text


class DashboardLayoutTests(unittest.TestCase):
    def test_scanner_status_distinguishes_first_pass_from_ready_refreshing(self):
        class Scanner:
            @staticmethod
            def is_alive():
                return True

        eyes = type("Eyes", (), {})()
        eyes.lock = threading.Lock()
        eyes.scan_summary = {"counts": {}, "player": "unknown",
                             "players": (), "pets": (), "monsters": ()}
        eyes.loot, eyes.scan_error, eyes.recovery = {}, "", ""
        eyes.scanner, eyes.classes = Scanner(), {"monster": 1}
        eyes.me = eyes.basis = eyes.chasing = None
        eyes.mode, eyes.routing = "no unit", False
        eyes.target_name = eyes.loot_name = ""
        eyes.loot_mode = "no loot"

        eyes.scan_passes = 0
        first = dashboard_snapshot(eyes, True, "no unit", memory_driving=False)
        self.assertEqual(first["memory"]["scanner"], "SCANNING FIRST PASS")

        eyes.scan_passes = 1
        ready = dashboard_snapshot(eyes, True, "no monster", memory_driving=False)
        self.assertEqual(ready["memory"]["scanner"], "READY - REFRESHING")
        self.assertIn("memory scan ready", ready["warning"])

    def sample(self):
        return {
            "running": True,
            "status": None,
            "bot_mode": "memory",
            "source": "pixels",
            "state": "memory rescan",
            "stick": (0.25, -1.0),
            "attack": True,
            "action": "lt",
            "target": "PIXEL red marker at 24.0",
            "navigation": "no unit / direct",
            "warning": "memory primary; pixels are temporary fallback",
            "memory": {
                "scanner": "running",
                "classes": "monster+player+pet+loot",
                "calibrated": False,
                "counts": "25 monster objects | 2 players | 1 pets",
                "player": "Lepica [YOU]",
                "players": ("Lepica [YOU]", "Guest"),
                "pets": ("Boarlet [YOURS]",),
                "monsters": ("Sun Lion x2", "Slime x4"),
            },
            "loot": {
                "detected": 5,
                "wanted": 3,
                "ground": ("Gold Ore x3 [WANTED]", "Axe x2 [filtered]"),
            },
        }

    def test_plain_dashboard_groups_user_facing_information(self):
        text = dashboard_text(self.sample(), color=False)
        for expected in (
                "SPIRITVALE COMBAT BOT", "OVERVIEW", "COMBAT & CONTROL",
                "MEMORY SCANNER", "WORLD ENTITIES", "LOOT", "NAVIGATION",
                "Status", "RUNNING", "Primary mode", "MEMORY",
                "Active source", "PIXELS", "Current task", "memory rescan",
                "Target", "PIXEL red marker at 24.0", "Controller",
                "Scanner", "Calibration", "WAITING", "Press END to stop",
                "WARNING", "temporary fallback"):
            self.assertIn(expected, text)
        self.assertNotIn("[filtered]", text)
        self.assertNotIn("Axe x2", text)
        self.assertNotIn("\x1b[", text)

    def test_every_dashboard_row_and_section_has_aligned_borders(self):
        lines = dashboard_text(self.sample(), color=False).splitlines()

        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertTrue(lines[0].startswith("╔") and lines[0].endswith("╗"))
        self.assertTrue(lines[-1].startswith("╚") and lines[-1].endswith("╝"))
        for line in lines[1:-1]:
            self.assertIn(line[0], "│├")
            self.assertIn(line[-1], "│┤")

    def test_color_dashboard_uses_ansi_and_resets_style(self):
        text = dashboard_text(self.sample(), color=True)
        self.assertIn("\x1b[", text)
        self.assertIn("\x1b[0m", text)
        self.assertTrue(text.endswith("\x1b[0m"))


if __name__ == "__main__":
    unittest.main()
