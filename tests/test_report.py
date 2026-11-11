import tempfile
import unittest
from pathlib import Path

from pageload.report import build_report

from .helpers import browser_visit, visit, write_run


class ReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_browser_report(self):
        run = write_run(self.tmp / "run", [browser_visit(1, 1000), browser_visit(2, 2000)],
                        {"mode": "browser", "url": "https://hartak.am/", "expectedVisits": 2, "totalRuntimeMs": 3000,
                         "viewport": "1440x900", "waitUntil": "load"})
        html = build_report(run).read_text(encoding="utf-8")
        self.assertIn("Page Load Report", html)
        self.assertIn("2 / 2", html)
        self.assertIn("Fully loaded", html)
        self.assertNotIn("did not finish", html)

    def test_missing_visits_warning_and_api_wording(self):
        run = write_run(self.tmp / "api", [visit(1, totalMs=50, ttfbMs=20, connectMs=1, tlsMs=2, receiveMs=3)],
                        {"mode": "api", "method": "POST", "url": "https://a.am/api", "expectedVisits": 4})
        html = build_report(run).read_text(encoding="utf-8")
        self.assertIn("API Response Report", html)
        self.assertIn("1 / 4", html)
        self.assertIn("3 of 4 requests did not finish", html)
        self.assertIn("API · POST", html)

    def test_escapes_untrusted_text(self):
        bad = browser_visit(1, 1000, passed=False, error="<script>alert(1)</script>")
        run = write_run(self.tmp / "x", [bad], {"mode": "browser", "url": "https://a.am/<b>"})
        html = build_report(run).read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)

    def test_empty_run(self):
        run = write_run(self.tmp / "empty", [], {"mode": "browser", "expectedVisits": 3})
        html = build_report(run).read_text(encoding="utf-8")
        self.assertIn("No visits recorded.", html)


if __name__ == "__main__":
    unittest.main()
