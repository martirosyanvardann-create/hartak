import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from pageload import export
from pageload.config import RunConfig
from pageload.job import Job, job_result_path
from pageload.report import build_report

from .helpers import FakeBrowser, FakeRunner, browser_visit, write_run


def finished_job(reports: Path, compare: bool = True) -> Job:
    visits_a = [browser_visit(i, 1000) for i in range(1, 4)]
    visits_b = [browser_visit(i, 3000) for i in range(1, 4)]
    runner = FakeRunner([visits_a, visits_b])
    raw = {"url": "https://hartak.am/", "vus": 1, "iterations": 3}
    if compare:
        raw["compareUrl"] = "https://dev.hartak.am/"
    job = Job(RunConfig.from_dict(raw), reports, runner_factory=lambda: runner,
              browser_factory=FakeBrowser, report_builder=build_report)
    job.run()
    return job


class ExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_result_is_saved_for_later_download(self):
        job = finished_job(self.reports)
        saved = export.find_job_result(self.reports, job.id)
        self.assertEqual(saved["id"], job.id)
        self.assertEqual(saved["download"], f"/api/jobs/{job.id}/download")
        self.assertTrue(job_result_path(self.reports, job.id).is_file())

    def test_unique_ids_within_one_second(self):
        first = finished_job(self.reports, compare=False)
        second = finished_job(self.reports, compare=False)
        self.assertNotEqual(first.id, second.id)

    def test_visits_csv(self):
        run = write_run(self.reports / "run", [browser_visit(1, 1234.56), browser_visit(2, 2000)],
                        {"mode": "browser"})
        rows = list(csv.DictReader(io.StringIO(export.visits_csv(run))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["fullyLoadedMs"], "1234.6")
        self.assertEqual(rows[0]["passed"], "True")
        self.assertIn("screenshot", rows[0])

    def test_comparison_zip(self):
        job = finished_job(self.reports)
        status = export.find_job_result(self.reports, job.id)
        names = set(zipfile.ZipFile(io.BytesIO(export.job_zip(status, self.reports))).namelist())
        a, b = job.targets[0].run_dir.name, job.targets[1].run_dir.name
        for name in ("comparison.html", "comparison.csv", "result.json",
                     f"{a}/report.html", f"{a}/visits.csv", f"{a}/console.log", f"{b}/visits.csv"):
            self.assertIn(name, names)
        self.assertEqual(export.job_zip_name(status), f"pageload-comparison-dev.hartak.am-vs-hartak.am-{job.id}.zip")

    def test_comparison_csv_and_html(self):
        status = finished_job(self.reports).status()
        rows = list(csv.reader(io.StringIO(export.results_csv(status))))
        self.assertEqual(rows[0][2:5], ["hartak.am median ms", "dev.hartak.am median ms", "dev.hartak.am vs hartak.am (median)"])
        fully = next(r for r in rows if r[0] == "fullyLoadedMs")
        self.assertEqual(fully[4], "3.0× slower")
        page = export.summary_html(status)
        self.assertIn("dev.hartak.am is 3.0× slower than hartak.am to fully load.", page)

    def test_single_url_zip(self):
        job = finished_job(self.reports, compare=False)
        status = job.status()
        names = zipfile.ZipFile(io.BytesIO(export.job_zip(status, self.reports))).namelist()
        self.assertIn("summary.html", names)
        self.assertIn("timings.csv", names)
        self.assertTrue(export.job_zip_name(status).startswith("pageload-result-hartak.am-"))

    def test_run_zip(self):
        run = write_run(self.reports / "20261110-010101-A-hartak.am", [browser_visit(1, 1000)], {"mode": "browser"})
        (run / "screenshots").mkdir()
        (run / "screenshots" / "fail.png").write_bytes(b"png")
        names = zipfile.ZipFile(io.BytesIO(export.run_zip(run))).namelist()
        self.assertIn(f"{run.name}/screenshots/fail.png", names)
        self.assertIn(f"{run.name}/visits.csv", names)

    def test_unsafe_names_are_refused(self):
        write_run(self.reports / "good-run", [browser_visit(1, 1000)])
        self.assertIsNotNone(export.find_run_dir(self.reports, "good-run"))
        for bad in ("..", "../etc", "good-run/..", "", ".hidden", "a/b", "missing"):
            self.assertIsNone(export.find_run_dir(self.reports, bad), bad)
        for bad in ("../x", "20261110", "abc", ""):
            self.assertIsNone(export.find_job_result(self.reports, bad), bad)

    def test_untrusted_text_is_escaped(self):
        status = finished_job(self.reports, compare=False).status()
        status["targets"][0]["url"] = "https://a.am/<script>x</script>"
        self.assertNotIn("<script>x", export.summary_html(status))


if __name__ == "__main__":
    unittest.main()
