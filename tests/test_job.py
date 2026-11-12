import tempfile
import threading
import unittest
from pathlib import Path

from pageload.config import RunConfig
from pageload.job import Job, JobState, diagnose
from pageload.k6 import K6Result

from .helpers import FakeBrowser, FakeRunner, browser_visit, no_report, visit, write_run


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


class HybridRunner:
    """Fake k6 for hybrid mode: the load run keeps going until it is stopped."""

    def __init__(self, browser_visits, load_visits):
        self.browser_visits, self.load_visits = browser_visits, load_visits
        self.calls = []
        self._stopped = threading.Event()

    def run(self, script, k6_vars, out_dir, *, extra_env=None, watchdog_s=None, on_line=lambda line: None):
        self.calls.append({"script": Path(script).name, "vars": dict(k6_vars)})
        if k6_vars.get("LOAD") == "1":
            write_run(out_dir, self.load_visits, {"mode": "api"})
            self._stopped.wait(10)
            return K6Result(exit_code=0, elapsed_s=1, stopped=True)
        write_run(out_dir, self.browser_visits, {"mode": "browser", "totalRuntimeMs": 4000,
                                                 "expectedVisits": len(self.browser_visits), "vus": 3})
        return K6Result(exit_code=0, elapsed_s=1)

    def stop(self):
        self._stopped.set()


class HybridJobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_browsers_measure_while_http_users_load(self):
        load = [visit(i, startedAt=f"2026-11-12T10:00:{i:02d}Z", ttfbMs=900, totalMs=1200) for i in range(1, 41)]
        load[0].update(passed=False, error="request: HTTP status 502, expected 2xx")
        runner = HybridRunner([browser_visit(i, 2000) for i in range(1, 7)], load)
        config = RunConfig.from_dict({"url": "https://hartak.am/", "vus": 30, "iterations": 6, "browsers": 3})
        job = Job(config, self.tmp, runner_factory=lambda: runner, browser_factory=FakeBrowser,
                  report_builder=no_report, load_warmup_s=0)
        job.run()
        self.assertIs(job.state, JobState.DONE)
        load_call, browser_call = runner.calls
        self.assertEqual((load_call["script"], load_call["vars"]["VUS"], load_call["vars"]["LOAD"]),
                         ("api-test.js", "27", "1"))
        self.assertEqual((browser_call["script"], browser_call["vars"]["VUS"]), ("browser-test.js", "3"))
        status = job.status()
        self.assertEqual((status["browsers"], status["loadUsers"]), (3, 27))
        stats = status["targets"][0]["stats"]
        self.assertEqual((stats["load"]["users"], stats["load"]["requests"], stats["load"]["failed"]), (27, 40, 1))
        titles = [f["title"] for f in stats["diagnosis"]["findings"]]
        self.assertIn("Background load: the server answered the HTTP load users", titles)
        self.assertTrue((job.targets[0].run_dir / "machine.json").exists())
