import unittest

from ui_bot.verify_originals import verify


class OriginalFileIntegrityTests(unittest.TestCase):
    def test_every_pre_ui_file_is_unchanged(self):
        self.assertEqual(verify(), [])


if __name__ == "__main__":
    unittest.main()
