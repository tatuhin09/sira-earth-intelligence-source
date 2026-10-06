from pathlib import Path
import unittest

class RepositoryHygieneTests(unittest.TestCase):
    def test_generated_runtime_directories_are_gitignored(self):
        root = Path(__file__).resolve().parents[1]
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        actual = {x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")}
        required = {".sira_update_backups/", "improvements/", "memory/", "runtime/"}
        self.assertTrue(required.issubset(actual), required - actual)

if __name__ == "__main__":
    unittest.main()
