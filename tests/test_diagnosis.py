import unittest
from datetime import datetime, timedelta, timezone

from pageload.diagnosis import classify, diagnose, readable_body
from pageload.diagnosis_html import render
from pageload.modes import get_mode

from .helpers import visit

T0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
TIMEOUT = 'navigation: navigating to "https://dev.hartak.am": timed out after 30s'


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def ok(n, second, ttfb=300, vu=None, **extra):
    return visit(n, startedAt=at(second), vu=vu or n % 10 + 1, ttfbMs=ttfb, fullyLoadedMs=ttfb + 1500, **extra)


def fail(n, second, error, status=None, vu=None, **extra):
    return visit(n, passed=False, startedAt=at(second), vu=vu or n % 10 + 1, httpStatus=status, error=error, **extra)


def titles(result):
    return [f["title"] for f in result["findings"]]


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            TIMEOUT: "timeout",
            "navigation: Error: HTTP status 502, expected 200": "bad_gateway",
            "request: HTTP status 404, expected 2xx": "not_found",
            "request: HTTP status 403, expected 2xx": "auth",
            "request: HTTP status 429, expected 2xx": "rate_limited",
            "request: HTTP status 405, expected 2xx": "client_error",
            "navigation: net::ERR_CONNECTION_RESET": "reset",
            "request: dial tcp: connect: connection refused": "refused",
            "navigation: net::ERR_NAME_NOT_RESOLVED": "dns",
            "request: x509: certificate has expired": "tls",
            "timings: Cannot find context with specified id": "browser",
        }
        for error, kind in cases.items():
            with self.subTest(error=error):
                self.assertEqual(classify({"passed": False, "error": error}), kind)
        self.assertIsNone(classify({"passed": True}))

    def test_readable_body(self):
        self.assertEqual(readable_body("<html><head><title>502 Bad Gateway</title></head></html>"),
                         'HTML page titled "502 Bad Gateway"')
        self.assertEqual(readable_body('{"message":"Cannot GET /api/home"}'), '{"message":"Cannot GET /api/home"}')


class DiagnoseTest(unittest.TestCase):
    browser, api = get_mode("browser"), get_mode("api")

    def test_healthy_run(self):
        visits = [ok(i, i, ttfb=300 + i) for i in range(1, 21)]
        result = diagnose(visits, mode=self.browser, vus=5)
        self.assertEqual(result["headline"], "No problems found.")
        self.assertFalse([f for f in result["findings"] if f["level"] != "info"])
        self.assertEqual(result["categories"], [])

    def test_wrong_url_404(self):
        visits = [fail(i, i * 0.1, "request: HTTP status 404, expected 2xx", 404, durationMs=300,
                       bodySnippet='{"message":"Cannot GET /api/home"}',
                       responseHeaders={"x-powered-by": "Express"}) for i in range(1, 31)]
        result = diagnose(visits, mode=self.api, vus=5)
        self.assertEqual(result["headline"], "The URL does not exist on the server (404 Not Found)")
        finding = result["findings"][0]
        self.assertEqual(finding["side"], "URL / configuration")
        self.assertIn("so it is up", finding["detail"])
        self.assertIn('Server answered: {"message":"Cannot GET /api/home"}', finding["evidence"])

    def test_outage_after_502(self):
        """The dev.hartak.am pattern: works for a while, 502s, then only timeouts until the end."""
        visits = [fail(i, i, TIMEOUT) for i in range(0, 20)]                              # slow start
        visits += [ok(100 + i, 20 + i, ttfb=1000 + i * 400) for i in range(10)]           # short window
        visits += [fail(200 + i, 30 + i * 0.2, "navigation: Error: HTTP status 502, expected 200", 502,
                        responseHeaders={"server": "nginx"},
                        bodySnippet="<html><head><title>502 Bad Gateway</title></head></html>") for i in range(3)]
        visits += [fail(300 + i, 32 + i, TIMEOUT) for i in range(40)]                     # never recovers
        result = diagnose(visits, mode=self.browser, vus=30)
        found = titles(result)
        self.assertTrue(result["headline"].startswith("The site stopped answering at about 0:29"), result["headline"])
        self.assertIn("The app behind the proxy stopped answering (502 Bad Gateway)", found)
        self.assertIn("Nothing worked for the first 20 s", found)
        self.assertTrue(any(t.startswith("The server did not answer within") for t in found))
        outage = result["findings"][0]
        self.assertIn("502 Bad Gateway", outage["detail"])
        gateway = next(f for f in result["findings"] if "502" in f["title"])
        self.assertIn("Answered by: nginx", gateway["evidence"])
        self.assertIn('Server answered: HTML page titled "502 Bad Gateway"', gateway["evidence"])
        # More browser users than recommended: the test computer is mentioned as a possible factor.
        self.assertIn("The test computer may also be a bottleneck", found)
        kinds = {c["key"]: c["count"] for c in result["categories"]}
        self.assertEqual(kinds, {"timeout": 60, "bad_gateway": 3})
        self.assertGreater(len(result["timeline"]["buckets"]), 2)

    def test_answered_together_after_long_wait(self):
        """30 users start together; all get the first byte after ~13 s at nearly the same moment."""
        visits = [ok(i, 0.01 * i, ttfb=13000 - 10 * i) for i in range(30)]
        result = diagnose(visits, mode=self.browser, vus=30)
        self.assertEqual(result["headline"], "Requests were answered all together after a long wait")

    def test_slower_under_load(self):
        visits = [ok(i, i, ttfb=400 + i * 150) for i in range(40)]
        found = titles(diagnose(visits, mode=self.browser, vus=10))
        self.assertIn("The server gets much slower under parallel load", found)

    def test_html_arrived_but_page_hung(self):
        visits = [fail(i, i, TIMEOUT, documentStatus=200,
                       failedRequests=[{"url": "https://cdn.example/app.js?v=1", "error": "net::ERR_TIMED_OUT",
                                        "type": "Script"}]) for i in range(10)]
        result = diagnose(visits, mode=self.browser, vus=5)
        found = titles(result)
        self.assertIn("The HTML arrived, but the page never finished loading", found)
        self.assertIn("Some of the page's resources fail", found)
        self.assertEqual(result["resources"]["failing"][0]["url"], "https://cdn.example/app.js")

    def test_missing_visits(self):
        visits = [ok(i, i) for i in range(5)]
        found = titles(diagnose(visits, mode=self.browser, vus=5, expected=8))
        self.assertIn("3 visits never finished", found)

    def test_render_escapes_and_includes_sections(self):
        visits = [fail(i, i, "<script>x</script> HTTP status 500", 500) for i in range(5)] + [ok(9, 9)]
        page = render(diagnose(visits, mode=self.browser, vus=2))
        self.assertIn("Failures by type", page)
        self.assertNotIn("<script>x", page)
        self.assertEqual(render(None), "")

    def test_no_visits(self):
        self.assertEqual(diagnose([], mode=self.api)["headline"], "No requests were recorded.")


if __name__ == "__main__":
    unittest.main()
