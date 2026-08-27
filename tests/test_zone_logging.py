import unittest

from ui_bot.runtime import ZoneRejectionLogLimiter


class ZoneRejectionLoggingTests(unittest.TestCase):
    def test_small_per_scan_count_churn_is_suppressed(self):
        limiter = ZoneRejectionLogLimiter(interval_s=15.0)
        self.assertTrue(limiter.allow("zone: rejected 195 monster(s) outside 'depth2'", 100.0))
        self.assertFalse(limiter.allow("zone: rejected 200 monster(s) outside 'depth2'", 101.0))

    def test_material_change_and_periodic_summary_are_logged(self):
        limiter = ZoneRejectionLogLimiter(interval_s=15.0)
        limiter.allow("zone: rejected 195 monster(s) outside 'depth2'", 100.0)
        self.assertTrue(limiter.allow("zone: rejected 150 monster(s) outside 'depth2'", 101.0))
        self.assertTrue(limiter.allow("zone: rejected 152 monster(s) outside 'depth2'", 116.0))

    def test_non_zone_output_is_never_suppressed(self):
        limiter = ZoneRejectionLogLimiter()
        self.assertTrue(limiter.allow("[Snapshot] seq=2 player_valid=True", 1.0))


if __name__ == "__main__":
    unittest.main()
