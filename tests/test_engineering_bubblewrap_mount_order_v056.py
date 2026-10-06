from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.engineering_verification import _bubblewrap_prefix


class EngineeringBubblewrapMountOrderV056Tests(unittest.TestCase):
    def test_tmpfs_is_mounted_before_candidate_bind_and_chdir(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            candidate = Path(tmp) / "candidate"
            candidate.mkdir()

            argv = _bubblewrap_prefix(candidate, "/usr/bin/bwrap")

        tmpfs_index = argv.index("--tmpfs")
        candidate_bind_index = argv.index(str(candidate)) - 1
        chdir_index = argv.index("--chdir")

        self.assertEqual(argv[tmpfs_index + 1], "/tmp")
        self.assertEqual(argv[candidate_bind_index], "--bind")
        self.assertEqual(argv[candidate_bind_index + 1:candidate_bind_index + 3], [
            str(candidate),
            str(candidate),
        ])
        self.assertLess(tmpfs_index, candidate_bind_index)
        self.assertLess(candidate_bind_index, chdir_index)


if __name__ == "__main__":
    unittest.main()
