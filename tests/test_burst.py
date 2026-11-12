import tempfile
import unittest
from pathlib import Path

from pageload.burst import Watcher, plan_result, round_result
from pageload.config import BurstPlan, RunConfig, ValidationError
from pageload.job import Job, JobState
from pageload.k6 import K6Result

from .helpers import FakeBrowser, no_report, visit, write_run


def ok(n, page_ms=800, first_ms=200):
    return visit(n, ttfbMs=first_ms, totalMs=page_ms - 100, pageMs=page_ms)


def failed(n, error="request: HTTP status 502, expected 2xx", status=502):
    return visit(n, passed=False, httpStatus=status, error=error)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ConfigTest(unittest.TestCase):
    def test_plan(self):
        c = RunConfig.from_dict({"mode": "burst", "url": "https://dev.hartak.am/", "burstUsers": "30, 10 20",
                                 "rounds": 2})
        self.assertEqual((c.burst.steps, c.burst.rounds, c.vus, c.iterations), ((10, 20, 30), 2, 30, 120))
        self.assertEqual(RunConfig.from_dict({"mode": "burst", "url": "https://a.am/"}).burst.steps, (30,))
        for bad in ("x", "0", "5000", ",".join(["1"] * 11)):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                BurstPlan.from_dict({"burstUsers": bad})

    def test_vars(self):
        c = RunConfig.from_dict({"mode": "burst", "url": "https://a.am/", "fullPage": False})
        v = c.mode.burst_vars(c, "https://a.am/", Path("/tmp/x"), 25)
        self.assertEqual((v["VUS"], v["ITERATIONS"], v["BROWSER_HEADERS"], v["FULL_PAGE"]), ("25", "25", "1", "0"))


class VerdictTest(unittest.TestCase):
    up = {"recovered": True, "afterS": 0}

    def test_verdicts(self):
        self.assertEqual(round_result([ok(i) for i in range(10)], 10, 1, self.up, 3000).verdict, "ok")
        self.assertEqual(round_result([ok(i, 5000, 4000) for i in range(10)], 10, 1, self.up, 3000).verdict, "slow")
        # A slow whole page with a fast server (slow test connection) is not "slow".
        self.assertEqual(round_result([ok(i, 9000, 300) for i in range(10)], 10, 1, self.up, 3000).verdict, "ok")
        r = round_result([ok(i) for i in range(8)] + [failed(9), failed(10)], 10, 1,
                         {"recovered": True, "afterS": 12}, 3000)
        self.assertEqual((r.verdict, r.failed, r.errors), ("failing", 2, {"502 Bad Gateway": 2}))
        self.assertIn("502", r.problem)
        down = round_result([failed(i) for i in range(10)], 10, 1, {"recovered": False, "afterS": None}, 3000)
        self.assertEqual(down.verdict, "down")

    def test_missing_requests_count_as_failed(self):
        r = round_result([ok(i) for i in range(7)], 10, 1, self.up, 3000)
        self.assertEqual((r.verdict, r.failed, r.errors["No answer recorded"]), ("failing", 3, 3))

    def test_answer(self):
        rounds = [round_result([ok(i) for i in range(10)], 10, n, self.up, 3000) for n in (1, 2)]
        rounds += [round_result([ok(i) for i in range(20)], 20, 1, self.up, 3000),
                   round_result([failed(i) for i in range(20)], 20, 2, {"recovered": True, "afterS": 40}, 3000)]
        result = plan_result(rounds, (10, 20), 2, {"ok": True})
        self.assertEqual(result["handledUsers"], 10)
        self.assertEqual(result["firstProblemUsers"], 20)
        self.assertEqual(result["answer"], "Errors at 20 users at the same moment. 10 users at once: handled in all "
                                           "2 rounds. 20 users at once: 1 handled, 1 failing (of 2 rounds); after "
                                           "the burst the site needed up to 40 s to answer again.")
        self.assertIn("check before the test failed", plan_result([], (10,), 1, {"ok": False})["answer"])


class CertificateTest(unittest.TestCase):
    def test_context_has_trusted_certificates(self):
        """python.org Python on macOS ships without certificates: the system bundle is used then."""
        from pageload.burst import ssl_context
        self.assertGreater(ssl_context().cert_store_stats().get("x509_ca", 0), 0)


class WatcherTest(unittest.TestCase):
    def test_recovery(self):
        clock = FakeClock()
        answers = iter([(False, 503, 10, "HTTP 503")] * 3 + [(True, 200, 300, None)])
        watcher = Watcher("https://x/", check=lambda url, t: next(answers), sleep=clock.sleep, clock=clock)
        result = watcher.recovery(60)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["afterS"], 9)          # three failed checks, 3 s apart
        self.assertEqual(len(result["checks"]), 4)

    def test_gives_up(self):
        clock = FakeClock()
        watcher = Watcher("https://x/", check=lambda url, t: (False, None, 10, "refused"), sleep=clock.sleep,
                          clock=clock)
        self.assertFalse(watcher.recovery(10)["recovered"])


class BurstRunner:
    """Fake k6: writes `users` requests per burst; bursts listed in `fail_at` all get 502."""

    def __init__(self, fail_at=()):
        self.fail_at = set(fail_at)
        self.calls = []

    def run(self, script, k6_vars, out_dir, **kwargs):
        users = int(k6_vars["VUS"])
        self.calls.append(users)
        visits = [failed(i) if (users, len(self.calls)) in self.fail_at or users in self.fail_at else ok(i)
                  for i in range(1, users + 1)]
        write_run(out_dir, visits, {"mode": "burst"})
        return K6Result(exit_code=0, elapsed_s=1)

    def stop(self):
        pass


class BurstJobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def job(self, runner, checks, **raw):
        clock = FakeClock()
        answers = iter(checks)
        config = RunConfig.from_dict({"mode": "burst", "url": "https://dev.hartak.am/", **raw})
        return Job(config, self.tmp, runner_factory=lambda: runner, browser_factory=FakeBrowser,
                   report_builder=no_report, url_check=lambda url, t: next(answers, (True, 200, 300, None)),
                   sleep=clock.sleep, clock=clock)

    def test_growing_bursts_and_stop_when_larger_ones_fail(self):
        runner = BurstRunner(fail_at={20})
        job = self.job(runner, [], burstUsers="10, 20, 30", rounds=2)
        job.run()
        self.assertIs(job.state, JobState.DONE)
        self.assertEqual(runner.calls, [10, 10, 20, 20])      # 30 skipped: 20 failed in every round
        status = job.status()
        burst = status["targets"][0]["stats"]["burst"]
        self.assertEqual([r["verdict"] for r in burst["rounds"]], ["ok", "ok", "failing", "failing"])
        self.assertEqual(burst["handledUsers"], 10)
        self.assertIn("Larger bursts were skipped", burst["answer"])
        self.assertFalse(status["ok"])
        self.assertEqual(status["targets"][0]["done"], 60)
        self.assertTrue((job.targets[0].run_dir / "burst.json").exists())

    def test_site_down_before_the_test(self):
        runner = BurstRunner()
        job = self.job(runner, [(False, 503, 10, "HTTP 503")])
        job.run()
        self.assertEqual(runner.calls, [])
        self.assertIn("check before the test failed", job.status()["targets"][0]["problem"])

    def test_site_goes_down_and_test_stops(self):
        runner = BurstRunner(fail_at={30})
        # before: up · after burst 1: never answers again within the recovery window
        job = self.job(runner, [(True, 200, 300, None)] + [(False, 503, 10, "HTTP 503")] * 100,
                       burstUsers="30", rounds=3, recoverySec=30)
        job.run()
        self.assertEqual(runner.calls, [30])
        burst = job.status()["targets"][0]["stats"]["burst"]
        self.assertEqual(burst["rounds"][0]["verdict"], "down")
        self.assertTrue(burst["answer"].startswith("Went down at 30 users at the same moment."), burst["answer"])
        self.assertIn("came back after", burst["answer"])        # the later watch saw it answer again
        self.assertGreater(burst["rounds"][0]["recovery"]["cameBackAfterS"], 30)

    def test_site_never_comes_back(self):
        runner = BurstRunner(fail_at={30})
        clock = FakeClock()
        calls = iter(range(10**6))
        config = RunConfig.from_dict({"mode": "burst", "url": "https://dev.hartak.am/", "burstUsers": "30",
                                      "recoverySec": 30})
        job = Job(config, self.tmp, runner_factory=lambda: runner, browser_factory=FakeBrowser,
                  report_builder=no_report, sleep=clock.sleep, clock=clock,
                  url_check=lambda url, t: (True, 200, 300, None) if next(calls) == 0 else (False, 502, 10, "HTTP 502"))
        job.run()
        burst = job.status()["targets"][0]["stats"]["burst"]
        self.assertIn("had not come back when the test ended", burst["answer"])
        self.assertIn("went down", job.status()["targets"][0]["problem"])


if __name__ == "__main__":
    unittest.main()
