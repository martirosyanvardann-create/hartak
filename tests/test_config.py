import unittest

from pageload.config import ApiRequest, RunConfig, Settings, ValidationError


def config(**overrides):
    raw = {"url": "https://hartak.am/", "vus": 5, "iterations": 30}
    raw.update(overrides)
    return RunConfig.from_dict(raw)


class RunConfigTest(unittest.TestCase):
    def test_minimal_browser_config(self):
        c = config()
        self.assertEqual(c.urls, ("https://hartak.am/",))
        self.assertEqual(c.mode.name, "browser")
        self.assertEqual(c.expected_status(), "200")
        self.assertEqual(c.settings, Settings())

    def test_compare_url_and_api_mode(self):
        c = config(compareUrl="https://dev.hartak.am/", apiOnly=True, vus=200, iterations=10000)
        self.assertEqual(len(c.urls), 2)
        self.assertEqual(c.mode.name, "api")
        self.assertEqual(c.expected_status(), "2xx")

    def test_above_recommended_runs_with_warning(self):
        c = config(vus=25, iterations=2000)
        warnings = c.warnings()
        self.assertEqual(len(warnings), 2)
        self.assertIn("25 parallel users is more than the recommended 20", warnings[0])
        self.assertIn("2000 visits per URL", warnings[1])
        self.assertEqual(c.to_dict()["warnings"], warnings)

    def test_within_recommended_has_no_warnings(self):
        self.assertEqual(config(vus=20, iterations=1000).warnings(), [])
        self.assertEqual(config(vus=200, iterations=10000, apiOnly=True).warnings(), [])

    def test_api_warning_mentions_the_tested_site(self):
        warning = config(vus=300, iterations=300, apiOnly=True).warnings()[0]
        self.assertIn("heavy traffic for the tested site", warning)
        self.assertIn("for API tests", warning)

    def test_hard_maximum_still_blocks_typos(self):
        with self.assertRaises(ValidationError):
            config(vus=201, iterations=1000)
        with self.assertRaises(ValidationError):
            config(vus=5001, iterations=10000, apiOnly=True)
        config(vus=201, iterations=1000, apiOnly=True)  # fine in API mode

    def test_all_errors_are_reported_together(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.from_dict({"url": "ftp://x", "vus": 0, "iterations": "abc", "waitUntil": "soon"})
        errors = ctx.exception.errors
        self.assertEqual(len(errors), 4, errors)
        self.assertIn("'ftp://x' is not an http(s) URL.", errors)

    def test_missing_fields(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.from_dict({})
        self.assertIn("URL is required.", ctx.exception.errors)
        self.assertIn("Parallel users is required.", ctx.exception.errors)

    def test_users_cannot_exceed_visits(self):
        with self.assertRaises(ValidationError) as ctx:
            config(vus=20, iterations=12)
        self.assertIn("can't be more than total visits (12)", str(ctx.exception))

    def test_decimal_numbers_are_rejected(self):
        with self.assertRaises(ValidationError):
            config(vus=2.5)

    def test_time_limit_automatic_and_fixed(self):
        # 30 visits / 5 users = 6 rounds of (30 s timeout + 30 s browser overhead) + 30 s
        self.assertEqual(config().time_limit_s(), 6 * 60 + 30)
        self.assertEqual(config(apiOnly=True).time_limit_s(), 6 * 35 + 30)
        self.assertEqual(config(settings={"timeLimitSec": 120}).time_limit_s(), 120)


class SettingsTest(unittest.TestCase):
    def test_defaults_for_empty_values(self):
        self.assertEqual(Settings.from_dict({"timeoutSec": "", "budgetP95Ms": None}), Settings())

    def test_valid_values(self):
        s = Settings.from_dict({"timeoutSec": 10, "expectStatus": "200, 301", "locale": "hy-AM",
                                "budgetP95Ms": 3000, "screenshots": False, "viewportWidth": 390})
        self.assertEqual((s.timeout_s, s.expect_status, s.locale, s.budget_p95_ms, s.screenshots, s.viewport_width),
                         (10, "200,301", "hy-AM", 3000, False, 390))

    def test_invalid_values(self):
        with self.assertRaises(ValidationError) as ctx:
            Settings.from_dict({"timeoutSec": 0, "timeLimitSec": 5, "expectStatus": "abc", "locale": "x y",
                                "viewportWidth": 99})
        self.assertEqual(len(ctx.exception.errors), 5, ctx.exception.errors)

    def test_status_formats(self):
        for good in ("200", "2xx", "200,301", "2XX, 304"):
            Settings.from_dict({"expectStatus": good})
        for bad in ("2x", "600", "200;301", "ok"):
            with self.assertRaises(ValidationError, msg=bad):
                Settings.from_dict({"expectStatus": bad})

    def test_round_trip(self):
        s = Settings(timeout_s=12, locale="en-US")
        self.assertEqual(Settings.from_dict(s.to_dict()), s)


class ApiRequestTest(unittest.TestCase):
    def test_header_lines(self):
        r = ApiRequest.from_dict({"method": "post", "headers": "Accept: application/json\n\nX-Token: a:b"})
        self.assertEqual(r.method, "POST")
        self.assertEqual(r.headers, {"Accept": "application/json", "X-Token": "a:b"})

    def test_header_json(self):
        r = ApiRequest.from_dict({"headers": '{"Accept": "text/html"}'})
        self.assertEqual(r.headers, {"Accept": "text/html"})

    def test_bad_input(self):
        with self.assertRaises(ValidationError) as ctx:
            ApiRequest.from_dict({"method": "FETCH", "headers": "NoColonHere"})
        self.assertEqual(len(ctx.exception.errors), 2)

    def test_empty_body_is_none(self):
        self.assertIsNone(ApiRequest.from_dict({"body": ""}).body)


if __name__ == "__main__":
    unittest.main()
