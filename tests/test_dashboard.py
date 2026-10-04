import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cc_dashboard", ROOT / "cc_dashboard.py")
DASHBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASHBOARD)


class DashboardContractTests(unittest.TestCase):
    def test_local_only_default(self):
        self.assertEqual(DASHBOARD.HOST, "127.0.0.1")

    def test_provider_order_and_autonomy_panel(self):
        html = DASHBOARD.HTML_PAGE
        codex = html.index("OpenAI • Codex / Work")
        autonomy = html.index("POS • Autonomy8")
        claude = html.index('<h2 style="color: #3fb950;">Claude</h2>')
        self.assertLess(codex, autonomy)
        self.assertLess(autonomy, claude)

    def test_provider_ui_has_no_active_probe(self):
        self.assertNotIn("codex_probe_btn", DASHBOARD.HTML_PAGE)
        self.assertNotIn("onclick=\"probeCodex()\"", DASHBOARD.HTML_PAGE)

    def test_required_metrics_are_present(self):
        html = DASHBOARD.HTML_PAGE
        for text in (
            "API-equivalent", "Indexed lifetime tokens", "tokens / 1% quota",
            "builders / reviewers", "commits", "Recent activity",
        ):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
