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

    def test_large_archive_hashes_without_unbounded_read(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "large.anvil"
            archive.write_bytes(b"evidence" * 400000)
            expected = recording_hashes([archive])
            verify_restore(expected, [archive])

    def test_invalid_manifest_paths_and_hashes_are_rejected(self):
        for name, digest in (("../recording.anvil", "a" * 64),
                             ("recording.anvil", "invalid"), ("recording.txt", "a" * 64)):
            with self.assertRaises(ValueError):
                verify_restore({name: digest}, [])

    def test_symlinked_archive_cannot_satisfy_restore_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.anvil"
            original.write_bytes(b"evidence")
            link = root / "recording.anvil"
            link.symlink_to(original)
            with self.assertRaises(ValueError):
                verify_restore({"recording.anvil": recording_hashes([original])["original.anvil"]}, [link])
