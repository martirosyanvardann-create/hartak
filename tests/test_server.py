import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from pageload.config import RunConfig
from pageload.job import Job
from pageload.modes import MODES
from pageload.server import JobManager, create_server, resolve_static

from .helpers import FakeBrowser, FakeRunner, browser_visit, no_report


class ResolveStaticTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self._tmp.name)
        (self.reports / "run1").mkdir()
        (self.reports / "run1" / "report.html").write_text("ok")

    def tearDown(self):
        self._tmp.cleanup()

    def test_allowed(self):
        self.assertEqual(resolve_static("/", self.reports, False).name, "index.html")
        self.assertEqual(resolve_static("/gui/app.js", self.reports, False).name, "app.js")
        self.assertEqual(resolve_static("/reports/run1/report.html", self.reports, False).name, "report.html")

    def test_refused(self):
        for path in ("/app.py", "/reports/../app.py", "/reports/%2e%2e/app.py", "/gui/../config.py",
                     "/reports/", "/reports/run1", "/gui/missing.js", "/reports/run1\\..\\x"):
            with self.subTest(path=path):
                self.assertIsNone(resolve_static(path, self.reports, False))

    def test_api_only_hides_gui(self):
        self.assertIsNone(resolve_static("/", self.reports, True))
        self.assertIsNone(resolve_static("/gui/app.js", self.reports, True))
        self.assertIsNotNone(resolve_static("/reports/run1/report.html", self.reports, True))


class ServerTest(unittest.TestCase):
    """Real HTTP requests against the server, with a fake k6 runner."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        reports = Path(self._tmp.name)
        visits = [browser_visit(i, 1000) for i in range(1, 3)]

        def job_factory(config: RunConfig, reports_dir: Path) -> Job:
            runner = FakeRunner([visits, visits])
            return Job(config, reports_dir, runner_factory=lambda: runner,
                       browser_factory=FakeBrowser, report_builder=no_report)

        self.server = create_server(0, reports, manager=JobManager(reports, job_factory))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_config(self):
        status, body = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["modes"]["browser"]["limits"]["recommendedVus"],
                         MODES["browser"].limits.recommended_vus)
        self.assertEqual(body["modes"]["browser"]["limits"]["vus"], 200)
        self.assertIn("timeoutSec", body["settings"]["ranges"])

    def test_healthz(self):
        status, body = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_validation_errors(self):
        status, body = self.request("/api/run", {"url": "ftp://x", "vus": 1, "iterations": 1,
                                                 "settings": {"locale": "x y"}})
        self.assertEqual(status, 400)
        self.assertEqual(len(body["errors"]), 2)

    def test_invalid_json(self):
        req = urllib.request.Request(self.base + "/api/run", data=b"[1,2]", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        with ctx.exception as error:
            self.assertEqual(error.code, 400)

    def test_run_and_wait(self):
        status, body = self.request("/api/run", {"url": "https://hartak.am/", "compareUrl": "https://dev.hartak.am/",
                                                 "vus": 1, "iterations": 2, "wait": True})
        self.assertEqual(status, 200)
        job = body["job"]
        self.assertEqual(job["state"], "done")
        self.assertTrue(job["ok"])
        self.assertEqual(job["comparison"]["main"]["text"], "the same")
        status, body = self.request("/api/status")
        self.assertEqual(body["job"]["state"], "done")

    def test_warnings_returned_but_run_starts(self):
        rec = MODES["browser"].limits.recommended_vus
        status, body = self.request("/api/run", {"url": "https://hartak.am/", "vus": rec + 5, "iterations": 25, "wait": True})
        # The run starts (the fake k6 writes only 2 of the 25 visits, so it ends as
        # "problem"), and the warning is reported, not turned into an error.
        self.assertEqual(status, 200)
        self.assertNotEqual(body["job"]["state"], "error")
        self.assertEqual(body["job"]["vus"], rec + 5)
        self.assertEqual(len(body["job"]["warnings"]), 1)
        self.assertIn(f"more than the recommended {rec}", body["job"]["warnings"][0])

    def download(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as res:
                return res.status, res.headers, res.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers, error.read()

    def test_downloads(self):
        _, body = self.request("/api/run", {"url": "https://hartak.am/", "compareUrl": "https://dev.hartak.am/",
                                            "vus": 1, "iterations": 2, "wait": True})
        job = body["job"]
        self.assertEqual(job["download"], f"/api/jobs/{job['id']}/download")

        status, headers, content = self.download(job["download"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("attachment; filename=\"pageload-comparison-dev.hartak.am-vs-hartak.am-", headers["Content-Disposition"])
        self.assertIn("comparison.csv", zipfile.ZipFile(io.BytesIO(content)).namelist())

        status, _, content = self.download(job["targets"][0]["stats"]["download"])
        self.assertEqual(status, 200)
        self.assertIn(f"{job['targets'][0]['runFolder']}/visits.csv", zipfile.ZipFile(io.BytesIO(content)).namelist())

        status, saved = self.request(f"/api/jobs/{job['id']}")
        self.assertEqual((status, saved["id"]), (200, job["id"]))

    def test_download_refuses_unknown_and_unsafe(self):
        for path in ("/api/jobs/20990101-000000/download", "/api/jobs/..%2F..%2Fapp/download",
                     "/api/runs/..%2Fpageload/download", "/api/runs/missing/download"):
            with self.subTest(path=path):
                status, _, _ = self.download(path)
                self.assertEqual(status, 404)

    def test_unknown_route(self):
        status, body = self.request("/api/nope", {})
        self.assertEqual(status, 404)

    def test_gui_served_with_no_cache(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as res:
            self.assertIn(b"Page Load Compare", res.read())
            self.assertEqual(res.headers["Cache-Control"], "no-cache")


if __name__ == "__main__":
    unittest.main()
