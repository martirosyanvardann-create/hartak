import json
import unittest
from pathlib import Path

from pageload.config import RunConfig
from pageload.modes import MODES, get_mode


class ModesTest(unittest.TestCase):
    def test_registry(self):
        self.assertEqual(set(MODES), {"browser", "api", "burst"})
        self.assertIs(get_mode(None), MODES["browser"])
        self.assertIs(get_mode("unknown"), MODES["browser"])

    def test_scripts_exist(self):
        for mode in MODES.values():
            self.assertTrue(mode.script.is_file(), mode.script)

    def test_main_metric_is_a_timing(self):
        for mode in MODES.values():
            self.assertIn(mode.main_metric, [t.field for t in mode.timings])

    def test_browser_vars(self):
        c = RunConfig.from_dict({"url": "https://a.am/", "vus": 2, "iterations": 4,
                                 "settings": {"locale": "hy-AM", "viewportWidth": 390, "viewportHeight": 844,
                                              "screenshots": False, "timeoutSec": 10}})
        v = c.mode.k6_vars(c, "https://a.am/", Path("/tmp/run"))
        self.assertEqual(v["PROFILE"], "parallel")
        self.assertEqual((v["VIEWPORT_WIDTH"], v["VIEWPORT_HEIGHT"], v["LOCALE"]), ("390", "844", "hy-AM"))
        self.assertEqual((v["SCREENSHOT_ON_FAIL"], v["TIMEOUT_MS"], v["EXPECT_STATUS"]), ("0", "10000", "200"))
        self.assertEqual(v["MAX_DURATION"], f"{c.time_limit_s()}s")
        self.assertNotIn("API_METHOD", v)

    def test_api_vars(self):
        c = RunConfig.from_dict({"url": "https://a.am/api", "vus": 2, "iterations": 4, "apiOnly": True,
                                 "method": "POST", "headers": "Accept: application/json", "body": "{}"})
        v = c.mode.k6_vars(c, "https://a.am/api", Path("/tmp/run"))
        self.assertEqual((v["API_METHOD"], v["API_BODY"], v["EXPECT_STATUS"]), ("POST", "{}", "2xx"))
        self.assertEqual(json.loads(v["API_HEADERS"]), {"Accept": "application/json"})
        self.assertNotIn("VIEWPORT_WIDTH", v)

    def test_describe_for_gui(self):
        d = MODES["api"].describe()
        self.assertEqual((d["limits"]["recommendedVus"], d["limits"]["vus"]), (200, 5000))
        self.assertEqual((d["limits"]["recommendedIterations"], d["limits"]["iterations"]), (10000, 1_000_000))
        self.assertTrue(d["limits"]["loadNote"])
        self.assertEqual(d["mainLabel"], "Total")


if __name__ == "__main__":
    unittest.main()
