import os
import time
import unittest
from unittest import mock

from pageload import machine
from pageload.diagnosis import diagnose
from pageload.machine import GB, Capacity, MachineMonitor, overloaded, recommended_browsers, summarize
from pageload.modes import get_mode

from .helpers import visit


class FakeSampler:
    def __init__(self, cpu=95.0, memory=60.0):
        self.values = (cpu, memory)

    def cpu(self):
        return self.values[0]

    def memory(self):
        return self.values[1]


class MachineTest(unittest.TestCase):
    def test_recommended_browsers_from_cores_and_memory(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(recommended_browsers(Capacity(cores=10, memory_bytes=16 * GB)), 10)
            self.assertEqual(recommended_browsers(Capacity(cores=10, memory_bytes=4 * GB)), 8)   # memory bound
            self.assertEqual(recommended_browsers(Capacity(cores=64, memory_bytes=256 * GB)), 20)  # capped
            self.assertEqual(recommended_browsers(Capacity(cores=1, memory_bytes=1 * GB)), 2)    # floor
        with mock.patch.dict(os.environ, {"PAGELOAD_RECOMMENDED_BROWSERS": "7"}):
            self.assertEqual(recommended_browsers(Capacity(cores=10, memory_bytes=16 * GB)), 7)

    def test_summary_and_overloaded(self):
        cap = Capacity(cores=4, memory_bytes=8 * GB)
        busy = summarize([(i, 98.0, 70.0) for i in range(10)], cap)
        self.assertEqual((busy["cpuMax"], busy["busyShare"], busy["memoryMax"]), (98.0, 1.0, 70.0))
        self.assertTrue(overloaded(busy))
        calm = summarize([(i, 20.0, 50.0) for i in range(10)], cap)
        self.assertFalse(overloaded(calm))
        self.assertTrue(overloaded(summarize([(1, 10.0, 97.0)], cap)))      # memory nearly full
        self.assertFalse(overloaded(None))

    def test_monitor_samples_in_background(self):
        with MachineMonitor(interval_s=0.01, sampler_factory=FakeSampler) as monitor:
            time.sleep(0.1)
        self.assertGreater(len(monitor.samples), 2)
        self.assertEqual(monitor.summary()["cpuMax"], 95.0)

    def test_real_sampler_does_not_fail(self):
        sampler = machine._sampler()
        sampler.cpu()
        time.sleep(0.05)
        for value in (sampler.cpu(), sampler.memory()):
            self.assertTrue(value is None or 0 <= value <= 100)

    def test_overloaded_computer_is_the_main_finding(self):
        visits = [visit(i, passed=False, error="navigation: timed out after 30s", documentStatus=200,
                        documentMs=3600) for i in range(30)]
        busy = summarize([(i, 99.0, 80.0) for i in range(30)], Capacity(cores=10, memory_bytes=8 * GB))
        result = diagnose(visits, mode=get_mode("browser"), vus=30, machine=busy)
        self.assertEqual(result["headline"], "The test computer was overloaded, so these results are not reliable")
        sides = {f["title"]: f["side"] for f in result["findings"]}
        self.assertEqual(sides["The HTML arrived, but the test computer could not finish the pages in time"],
                         "test computer")
        self.assertNotIn("The HTML arrived, but the page never finished loading", sides)

    def test_without_machine_data_fast_html_and_many_browsers_points_to_computer(self):
        """Old runs (no machine.json): the 30-user hartak.am pattern."""
        visits = [visit(i, passed=False, error="navigation: timed out after 30s", documentStatus=200,
                        documentMs=3600) for i in range(30)]
        result = diagnose(visits, mode=get_mode("browser"), vus=30)
        self.assertEqual(result["headline"], "The HTML arrived, but the test computer could not finish the pages in time")


    def test_network_ceiling_in_summary(self):
        cap = Capacity(cores=4, memory_bytes=8 * GB)
        flat = summarize([(i, 20.0, 50.0, 22.0 if 2 <= i <= 18 else 3.0) for i in range(20)], cap)
        self.assertEqual((flat["netMaxMbps"], flat["netNearMaxShare"]), (22.0, 0.85))
        old = summarize([(i, 20.0, 50.0) for i in range(5)], cap)            # samples without network
        self.assertIsNone(old["netMaxMbps"])

    def test_monitor_measures_download_speed(self):
        class Counting(FakeSampler):
            total = 0

            def received_bytes(self):
                Counting.total += 125_000          # 1 Mbit per sample
                return Counting.total

        with MachineMonitor(interval_s=0.02, sampler_factory=Counting) as monitor:
            time.sleep(0.15)
        self.assertGreater(monitor.summary()["netMaxMbps"], 1)

    def network_run(self, receive_ms, flat_share):
        visits = [visit(i, ttfbMs=600, receiveMs=receive_ms, totalMs=600 + receive_ms,
                        transferBytes=1_811_383) for i in range(15)]
        visits += [visit(100 + i, passed=False, error="request: request timeout") for i in range(15)]
        samples = [(i, 15.0, 50.0, 24.0 if i < flat_share * 30 else 2.0) for i in range(30)]
        busy = summarize(samples, Capacity(cores=10, memory_bytes=8 * GB))
        return diagnose(visits, mode=get_mode("api"), vus=30, machine=busy)

    def test_connection_limit_found(self):
        """The dev.hartak.am burst on a phone hotspot: fast first byte, slow 1.8 MB downloads, flat speed."""
        result = self.network_run(receive_ms=17000, flat_share=0.8)
        finding = next(f for f in result["findings"] if "internet connection" in f["title"])
        self.assertEqual((finding["side"], finding["level"]), ("test computer", "critical"))
        self.assertIn("about 24 Mbit/s", finding["title"])
        self.assertIn("first byte median 600 ms", finding["detail"])

    def test_no_connection_limit_without_a_ceiling_or_slow_downloads(self):
        for receive, flat in ((17000, 0.1), (300, 0.8)):
            with self.subTest(receive=receive, flat=flat):
                titles = [f["title"] for f in self.network_run(receive, flat)["findings"]]
                self.assertFalse(any("internet connection" in t for t in titles))


if __name__ == "__main__":
    unittest.main()
