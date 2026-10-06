from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.desktop_app import DesktopControl
from sira.runtime import RuntimeStateStore


class DesktopBrandingTests(unittest.TestCase):
    def test_brand_assets_and_v02_navigation_exist(self):
        svg = ROOT / "desktop" / "assets" / "sira.svg"
        html = ROOT / "desktop" / "static" / "index.html"
        css = ROOT / "desktop" / "static" / "styles.css"
        js = ROOT / "desktop" / "static" / "app.js"

        self.assertTrue(svg.is_file())
        self.assertIn("<svg", svg.read_text(encoding="utf-8"))
        page = html.read_text(encoding="utf-8")
        self.assertIn('src="/sira.svg"', page)
        for name in (
            "Research",
            "Providers",
            "Promotions",
            "Tests & Health",
            "Chat with SIRA",
        ):
            self.assertIn(name, page)
        self.assertIn("SIRA Control Center v0.4", page)
        css_text = css.read_text(encoding="utf-8")
        self.assertIn(".dashboard-grid", css_text)
        self.assertIn(".research-history", css_text)
        self.assertIn("research-send-btn", page)
        self.assertIn("search-free-confirm", page)
        script = js.read_text(encoding="utf-8")
        self.assertIn("humanTime", script)
        self.assertIn("renderProviders", script)
        self.assertIn("renderPromotions", script)

    def test_overview_contract_stays_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            RuntimeStateStore(root).save({
                "desired_state": "off",
                "worker_state": "stopped",
                "pid": None,
                "started_at": "2026-09-21T10:00:00+00:00",
                "heartbeat_at": "2026-09-21T10:00:00+00:00",
                "generation": 8,
            })
            report = DesktopControl(root).overview()
            self.assertEqual(
                report["runtime"]["effective_state"],
                "stopped",
            )
            self.assertIn("memory", report)
            self.assertIn("activity", report)


if __name__ == "__main__":
    unittest.main()
