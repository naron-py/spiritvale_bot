import hashlib
import tempfile
import unittest
from pathlib import Path

import spiritvale_run_in_background_patch as patcher


class PatchLifecycleTest(unittest.TestCase):
    def test_enable_creates_exact_backup_and_restore_recovers_original(self):
        original = b"header\x00tail"
        expected_hash = hashlib.sha256(original).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "globalgamemanagers"
            backup = Path(directory) / "globalgamemanagers.backup"
            target.write_bytes(original)

            result = patcher.enable(
                target,
                backup,
                offset=6,
                expected_hash=expected_hash,
                process_running=lambda: False,
            )

            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(target.read_bytes(), b"header\x01tail")
            self.assertEqual(result.changed_offsets, (6,))

            patcher.restore(
                target,
                backup,
                offset=6,
                expected_original_hash=expected_hash,
                process_running=lambda: False,
            )

            self.assertEqual(target.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
