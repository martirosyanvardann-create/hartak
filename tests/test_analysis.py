import unittest

from pageload.analysis import budget_check, compare, describe, diff, percentile, result_summary, target_stats
from pageload.modes import MODES

from .helpers import browser_visit, visit


class PercentileTest(unittest.TestCase):
    def test_k6_linear_interpolation(self):
        values = [10, 20, 30, 40]
        self.assertEqual(percentile(values, 50), 25)
        self.assertAlmostEqual(percentile(values, 95), 38.5)
        self.assertEqual(percentile([7], 95), 7)
        self.assertIsNone(percentile([], 50))

    def test_describe(self):
        d = describe([3, 1, 2])
        self.assertEqual((d["med"], d["min"], d["max"], d["avg"]), (2, 1, 3, 2))


class DiffTest(unittest.TestCase):
    CASES = [
        (1000, 1030, "slower", "3% slower"),
        (1000, 960, "faster", "4% faster"),
        (1000, 1002, "slower", "1% slower"),      # tiny differences still show, at least 1%
        (1000, 1000, "same", "the same"),         # only exactly equal is "the same"
        (1000, 1300, "slower", "30% slower"),
        (2060, 6630, "slower", "3.2× slower"),
        (6630, 2060, "faster", "3.2× faster"),
        (1000, 0, "faster", "100% faster"),
        (None, 5, None, "—"),
        (0, 5, None, "—"),
    ]

    def test_cases(self):
        for a, b, verdict, text in self.CASES:
            with self.subTest(a=a, b=b):
                result = diff(a, b)
                self.assertEqual((result["verdict"], result["text"]), (verdict, text))


class SummaryTest(unittest.TestCase):
    def test_all_slower(self):
        self.assertEqual(result_summary("a.am", "b.am", ["slower"] * 6),
                         "b.am was slower than a.am on all 6 timings (median).")

    def test_all_faster(self):
        self.assertIn("faster than a.am on all 6", result_summary("a.am", "b.am", ["faster"] * 6))

    def test_mixed_names_both_sites(self):
        text = result_summary("a.am", "b.am", ["slower", "slower", "slower", "faster", "faster", "same"])
        self.assertEqual(text, "Compared with a.am, b.am was slower on 3, faster on 2 and the same on 1 of 6 timings (median).")

    def test_missing_values(self):
        self.assertIn("all 5 measured", result_summary("a.am", "b.am", ["slower"] * 5 + [None]))
        self.assertEqual(result_summary("a", "b", [None, None]), "Not enough data to compare the timings.")


class StatsAndCompareTest(unittest.TestCase):
    mode = MODES["browser"]

    def stats(self, *full_ms):
        visits = [browser_visit(i + 1, ms) for i, ms in enumerate(full_ms)]
        return target_stats(visits, {"totalRuntimeMs": 9000}, self.mode)

    def test_target_stats(self):
        visits = [browser_visit(1, 1000), browser_visit(2, 2000), visit(3, passed=False)]
        s = target_stats(visits, {"totalRuntimeMs": 6000}, self.mode)
        self.assertEqual((s["visits"], s["passed"], s["failed"]), (3, 2, 1))
        self.assertEqual(s["timings"]["fullyLoadedMs"]["med"], 1500)
        self.assertEqual(s["visitsPerMin"], 30)
        self.assertEqual(len(s["errors"]), 1)

    def test_compare(self):
        result = compare("https://hartak.am/", self.stats(1000, 1000), "https://dev.hartak.am/", self.stats(3000, 3000), self.mode)
        self.assertEqual(result["headline"], "dev.hartak.am is 3.0× slower than hartak.am to fully load.")
        self.assertEqual(result["main"]["text"], "3.0× slower")
        self.assertEqual(result["summary"], "dev.hartak.am was slower than hartak.am on all 6 timings (median).")
        self.assertEqual([r["metric"] for r in result["rows"]][-2:], ["runtimeMs", "transferMed"])

    def test_api_mode_wording(self):
        api = MODES["api"]
        stats = target_stats([visit(1, totalMs=50, ttfbMs=20, connectMs=0, tlsMs=0, receiveMs=5)], {}, api)
        slow = target_stats([visit(1, totalMs=500, ttfbMs=400, connectMs=0, tlsMs=0, receiveMs=50)], {}, api)
        result = compare("https://a.am/", stats, "https://b.am/", slow, api)
        self.assertTrue(result["headline"].endswith("to respond."))
        self.assertEqual(result["rows"][-1]["label"], "Response size")

    def test_budget(self):
        s = self.stats(1000, 2000, 3000)
        self.assertTrue(budget_check(s, self.mode, 5000)["passed"])
        self.assertFalse(budget_check(s, self.mode, 1500)["passed"])
        self.assertIsNone(budget_check(s, self.mode, 0))
        self.assertIsNone(budget_check(None, self.mode, 1000))


if __name__ == "__main__":
    unittest.main()
