from pathlib import Path
import tempfile
import unittest

from scripts.backup_verify import recording_hashes, verify_restore


class BackupVerifyTests(unittest.TestCase):
    def test_restored_archive_hashes_match_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "recording.anvil"
            archive.write_bytes(b"recorded evidence")
            expected = recording_hashes([archive])
            verify_restore(expected, [archive])
            archive.write_bytes(b"tampered evidence")
            with self.assertRaises(ValueError):
                verify_restore(expected, [archive])

    def test_missing_restore_fails_closed(self):
        with self.assertRaises(ValueError):
            verify_restore({"recording.anvil": "a" * 64}, [])
