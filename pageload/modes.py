"""Test modes (Strategy pattern).

Everything that differs between a browser test and an API test lives in one
class per mode: k6 script, limits, timings, labels and the k6 variables it
needs. The rest of the code asks the mode instead of checking ``mode == "api"``.
A new kind of test is one new subclass registered in :data:`MODES`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .machine import recommended_browsers
from .paths import K6_DIR

if TYPE_CHECKING:  # pragma: no cover
    from .config import RunConfig


@dataclass(frozen=True)
class Timing:
    """One measured timing: field name in the visit record, label, description."""

    field: str
    label: str
    description: str


@dataclass(frozen=True)
class Limits:
    """Recommended limits only warn; the maximums only stop obvious typos."""

    recommended_vus: int
    recommended_iterations: int
    max_vus: int
    max_iterations: int
    load_note: str      # why going above the recommendation can distort results


class TestMode(ABC):
    """What a kind of test needs to know about itself."""

    name: str
    title: str
    kind: str                  # used in sentences: "for browser tests" / "for API tests"
    script_name: str
    noun: str                  # "visits" / "requests"
    noun_singular: str
    action: str                # used in "B is 2× slower than A <action>"
    timings: tuple[Timing, ...]
    main_metric: str
    limits: Limits
    default_expect_status: str
    overhead_per_visit_s: int  # time on top of the timeout, for the run time limit
    needs_browser: bool

    @property
    def script(self) -> Path:
        return K6_DIR / self.script_name

    @property
    def main_label(self) -> str:
        return next(t.label for t in self.timings if t.field == self.main_metric)

    def k6_vars(self, config: "RunConfig", url: str, report_dir: Path) -> dict[str, str]:
        """All ``-e NAME=value`` variables for one k6 run of this mode."""
        settings = config.settings
        common = {
            "BASE_URL": url,
            "VUS": str(config.vus),
            "ITERATIONS": str(config.iterations),
            "TIMEOUT_MS": str(settings.timeout_s * 1000),
            "EXPECT_STATUS": settings.expect_status or self.default_expect_status,
            "MAX_DURATION": f"{config.time_limit_s()}s",
            "GRACEFUL_STOP": f"{config.graceful_stop_s}s",
            "REPORT_DIR": str(report_dir),
        }
        return {**common, **self._mode_vars(config)}

    @abstractmethod
    def _mode_vars(self, config: "RunConfig") -> dict[str, str]:
        """Variables only this mode uses."""

    def describe(self) -> dict:
        """Mode metadata for the GUI (served by GET /api/config)."""
        return {
            "name": self.name,
            "title": self.title,
            "kind": self.kind,
            "noun": self.noun,
            "nounSingular": self.noun_singular,
            "limits": {
                "vus": self.limits.max_vus,
                "iterations": self.limits.max_iterations,
                "recommendedVus": self.limits.recommended_vus,
                "recommendedIterations": self.limits.recommended_iterations,
                "loadNote": self.limits.load_note,
            },
            "timings": [[t.field, t.label, t.description] for t in self.timings],
            "mainMetric": self.main_metric,
            "mainLabel": self.main_label,
            "defaultExpectStatus": self.default_expect_status,
        }


class BrowserMode(TestMode):
    name = "browser"
    title = "Browser"
    kind = "browser"
    script_name = "browser-test.js"
    noun = "visits"
    noun_singular = "visit"
    action = "to fully load"
    timings = (
        Timing("ttfbMs", "TTFB", "first byte from the server"),
        Timing("fcpMs", "FCP", "first content painted"),
        Timing("lcpMs", "LCP", "largest content painted"),
        Timing("domMs", "DOM loaded", "DOMContentLoaded event"),
        Timing("loadMs", "Load", "browser load event finished"),
        Timing("fullyLoadedMs", "Fully loaded", "load event or last request, whichever is later"),
    )
    main_metric = "fullyLoadedMs"
    # Each user runs a real Chrome page on this computer: the recommendation comes from its
    # cores and memory (pageload.machine). Above it the computer, not the site, sets the timings.
    limits = Limits(
        recommended_vus=recommended_browsers(), recommended_iterations=1000, max_vus=200, max_iterations=100_000,
        load_note="each browser user is a real Chrome page on this computer, so more of them measure this "
                  "computer's CPU and memory instead of the site. For more users use hybrid mode: a few real "
                  "browsers measure while the other users create the load with plain HTTP requests",
    )
    default_expect_status = "200"
    overhead_per_visit_s = 30
    needs_browser = True

    def _mode_vars(self, config: "RunConfig") -> dict[str, str]:
        settings = config.settings
        variables = {
            "PROFILE": "parallel",
            "VUS": str(config.measuring_users),     # hybrid mode: only the real browsers
            "WAIT_UNTIL": config.wait_until,
            "VIEWPORT_WIDTH": str(settings.viewport_width),
            "VIEWPORT_HEIGHT": str(settings.viewport_height),
            "SCREENSHOT_ON_FAIL": "1" if settings.screenshots else "0",
        }
        if settings.locale:
            variables["LOCALE"] = settings.locale
        return variables


LOAD_PACE_MS = 5000     # hybrid mode: one page request per load user every 5 s


def load_k6_vars(config: "RunConfig", url: str, report_dir: Path) -> dict[str, str]:
    """Hybrid mode: variables for the background HTTP load (api-test.js with LOAD=1).

    Each load user requests the page about once every LOAD_PACE_MS (like one browser
    visit), until the tool stops the load when the measured browser visits are done.
    """
    return {
        "LOAD": "1",
        "LOAD_PACE_MS": str(LOAD_PACE_MS),
        "BASE_URL": url,
        "VUS": str(config.load_users),
        "ITERATIONS": "1",
        "TIMEOUT_MS": str(config.settings.timeout_s * 1000),
        "EXPECT_STATUS": config.settings.expect_status or "2xx",
        "MAX_DURATION": f"{config.time_limit_s() + config.graceful_stop_s + 60}s",
        "REPORT_DIR": str(report_dir),
        "API_METHOD": "GET",
        "API_HEADERS": "{}",
    }


class ApiMode(TestMode):
    name = "api"
    title = "API"
    kind = "API"
    script_name = "api-test.js"
    noun = "requests"
    noun_singular = "request"
    action = "to respond"
    timings = (
        Timing("connectMs", "Connect", "opening the TCP connection"),
        Timing("tlsMs", "TLS", "https handshake"),
        Timing("ttfbMs", "TTFB", "waiting for the first byte (server time)"),
        Timing("receiveMs", "Receive", "downloading the answer"),
        Timing("totalMs", "Total", "whole request, start to finish"),
    )
    main_metric = "totalMs"
    # No browser: much lighter, but many users can overload the tested site.
    limits = Limits(
        recommended_vus=200, recommended_iterations=10_000, max_vus=5000, max_iterations=1_000_000,
        load_note="this is heavy traffic for the tested site; make sure it is allowed, especially in production",
    )
    default_expect_status = "2xx"
    overhead_per_visit_s = 5
    needs_browser = False

    def _mode_vars(self, config: "RunConfig") -> dict[str, str]:
        request = config.request
        variables = {
            "API_METHOD": request.method,
            "API_HEADERS": json.dumps(request.headers),
        }
        if request.body:
            variables["API_BODY"] = request.body
        return variables


class BurstMode(ApiMode):
    """N separate users request the page at the same moment, in rounds (see pageload.burst).

    Each user asks like a browser does (browser headers, compression) and, with
    ``full_page``, also downloads the page's own scripts, styles and images. No Chrome
    runs, so the test computer is not the limit.
    """

    name = "burst"
    title = "Burst"
    kind = "burst"
    noun = "requests"
    noun_singular = "request"
    action = "to answer a burst"
    timings = ApiMode.timings + (
        Timing("pageMs", "Whole page", "the HTML plus the page's own scripts, styles and images"),
    )
    main_metric = "pageMs"
    limits = Limits(
        recommended_vus=100, recommended_iterations=3000, max_vus=1000, max_iterations=100_000,
        load_note="large bursts are heavy traffic for the tested site; make sure it is allowed, "
                  "especially in production",
    )
    default_expect_status = "2xx"
    overhead_per_visit_s = 30

    def burst_vars(self, config: "RunConfig", url: str, report_dir: Path, users: int) -> dict[str, str]:
        """k6 variables for one burst of ``users`` simultaneous requests."""
        variables = self.k6_vars(config, url, report_dir)
        variables.update({
            "VUS": str(users),
            "ITERATIONS": str(users),
            "BROWSER_HEADERS": "1",
            "FULL_PAGE": "1" if config.burst and config.burst.full_page else "0",
            "MAX_DURATION": f"{config.settings.timeout_s * 2 + 30}s",
            "API_METHOD": "GET",
        })
        return variables


MODES: dict[str, TestMode] = {mode.name: mode for mode in (BrowserMode(), ApiMode(), BurstMode())}


def get_mode(name: str | None) -> TestMode:
    """Mode by name; unknown or empty names mean the browser test."""
    return MODES.get(name or "browser", MODES["browser"])
