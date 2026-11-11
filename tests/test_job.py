import tempfile
import threading
import unittest
from pathlib import Path

from pageload.config import RunConfig
from pageload.job import Job, JobState, diagnose
from pageload.k6 import K6Result

from .helpers import FakeBrowser, FakeRunner, browser_visit, no_report


def make_job(tmp: Path, runner: FakeRunner, **raw) -> Job:
    config = RunConfig.from_dict({"url": "https://hartak.am/", "vus": 2, "iterations": 4, **raw})
    return Job(config, tmp, runner_factory=lambda: runner, browser_factory=FakeBrowser, report_builder=no_report)


class JobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_successful_comparison(self):
        fast = [browser_visit(i, 1000) for i in range(1, 5)]
        slow = [browser_visit(i, 3000) for i in range(1, 5)]
        runner = FakeRunner([fast, slow])
        job = make_job(self.tmp, runner, compareUrl="https://dev.hartak.am/")
        job.run()

        self.assertIs(job.state, JobState.DONE)
        status = job.status()
        self.assertTrue(status["ok"])
        self.assertEqual([t["phase"] for t in status["targets"]], ["done", "done"])
        self.assertEqual(status["comparison"]["main"]["text"], "3.0× slower")
        self.assertIn("fake k6 finished", status["log"])
        # The shared browser was passed to k6 and closed again.
        self.assertTrue(runner.calls[0]["env"]["K6_BROWSER_WS_URL"].startswith("ws://"))
        self.assertEqual(runner.calls[0]["vars"]["BASE_URL"], "https://hartak.am/")

    def test_api_mode_uses_no_browser(self):
        before = FakeBrowser.instances
        runner = FakeRunner([[browser_visit(i, 50, totalMs=50) for i in range(1, 5)]], mode="api")
        job = make_job(self.tmp, runner, apiOnly=True)
        job.run()
        self.assertEqual(FakeBrowser.instances, before)
        self.assertNotIn("K6_BROWSER_WS_URL", runner.calls[0]["env"])
        self.assertTrue(runner.calls[0]["script"].name.startswith("api"))

    def test_missing_visits_are_a_problem_not_done(self):
        runner = FakeRunner([[browser_visit(1, 1000), browser_visit(2, 1000)]],
                            result={"elapsed_s": 500}, expected=4)
        job = make_job(self.tmp, runner)
        job.run()
        self.assertIs(job.state, JobState.PROBLEM)
        target = job.status()["targets"][0]
        self.assertEqual(target["phase"], "incomplete")
        self.assertIn("2 of 4 visits hung", target["problem"])
        self.assertFalse(job.status()["ok"])
        titles = [f["title"] for f in target["stats"]["diagnosis"]["findings"]]
        self.assertIn("2 visits never finished", titles)

    def test_budget_makes_run_not_ok(self):
        runner = FakeRunner([[browser_visit(i, 3000) for i in range(1, 5)]])
        job = make_job(self.tmp, runner, settings={"budgetP95Ms": 1000})
        job.run()
        status = job.status()
        self.assertIs(job.state, JobState.DONE)
        self.assertFalse(status["targets"][0]["budget"]["passed"])
        self.assertFalse(status["ok"])

    def test_stop_before_second_url(self):
        runner = FakeRunner([[browser_visit(i, 1000) for i in range(1, 5)]] * 2)
        job = make_job(self.tmp, runner, compareUrl="https://dev.hartak.am/")
        original = runner.run

        def run_then_stop(*args, **kwargs):
            result = original(*args, **kwargs)
            job.stop()
            return result

        runner.run = run_then_stop
        job.run()
        self.assertIs(job.state, JobState.STOPPED)
        self.assertEqual(len(runner.calls), 1)

    def test_missing_k6_is_an_error(self):
        config = RunConfig.from_dict({"url": "https://hartak.am/", "vus": 1, "iterations": 1})
        job = Job(config, self.tmp, browser_factory=FakeBrowser, report_builder=no_report)
        import pageload.job as job_module
        original, job_module.find_k6 = job_module.find_k6, lambda: None
        try:
            job.run()
        finally:
            job_module.find_k6 = original
        self.assertIs(job.state, JobState.ERROR)
        self.assertIn("k6 was not found", job.message)

    def test_final_states_cannot_change(self):
        runner = FakeRunner([[browser_visit(i, 1000) for i in range(1, 5)]])
        job = make_job(self.tmp, runner)
        job.run()
        with self.assertRaises(RuntimeError):
            job._transition(JobState.STOPPED)

    def test_status_is_safe_while_running(self):
        started = threading.Event()
        release = threading.Event()
        runner = FakeRunner([[browser_visit(i, 1000) for i in range(1, 5)]])
        original = runner.run

        def slow_run(*args, **kwargs):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)

        runner.run = slow_run
        job = make_job(self.tmp, runner)
        worker = threading.Thread(target=job.run)
        worker.start()
        started.wait(5)
        status = job.status()
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["targets"][0]["phase"], "running")
        release.set()
        worker.join(5)
        self.assertIs(job.state, JobState.DONE)


class DiagnoseTest(unittest.TestCase):
    def result(self, **kw):
        return K6Result(exit_code=0, elapsed_s=kw.pop("elapsed_s", 5), **kw)

    def test_nothing_missing(self):
        self.assertIsNone(diagnose(4, 4, "visits", self.result(), 60))

    def test_connection_failure(self):
        r = self.result(errors=["error building browser: connecting to browser: broken pipe"])
        self.assertIn("could not connect to Chrome", diagnose(0, 40, "visits", r, 60))

    def test_hung(self):
        self.assertIn("hung", diagnose(2, 4, "requests", self.result(elapsed_s=61), 60))

    def test_watchdog(self):
        self.assertIn("stopped responding", diagnose(1, 4, "visits", self.result(watchdog_fired=True), 60))

    def test_stopped_is_not_a_problem(self):
        self.assertIsNone(diagnose(1, 4, "visits", self.result(stopped=True), 60))


if __name__ == "__main__":
    unittest.main()
