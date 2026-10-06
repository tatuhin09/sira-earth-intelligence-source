from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DesktopRefreshV042Tests(unittest.TestCase):
    def test_removed_legacy_research_element_is_guarded(self):
        script = (
            ROOT / "desktop" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'if ($("research-detail")) renderResearch(data);',
            script,
        )
        self.assertNotIn(
            '\n  renderResearch(data);\n',
            script,
        )


if __name__ == "__main__":
    unittest.main()
