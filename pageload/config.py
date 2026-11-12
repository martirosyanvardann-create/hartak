"""Run configuration as validated, immutable value objects.

All input checks live here. GUI, HTTP API and command line all build a
:class:`RunConfig` with :meth:`RunConfig.from_dict`; once it exists, the rest
of the code can trust it. Field names in dictionaries are the camelCase names
used by the HTTP API and the GUI.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .modes import MODES, TestMode, get_mode

WAIT_MODES = ("load", "networkidle")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
GRACEFUL_STOP_S = 30

_URL = re.compile(r"^https?://[^\s/]+", re.I)
_STATUS = re.compile(r"^[1-5](\d\d|xx)(,[1-5](\d\d|xx))*$", re.I)
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


class ValidationError(ValueError):
    """Invalid input. ``errors`` holds every problem found, not just the first."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(" ".join(errors))


def _int_in_range(raw: Mapping[str, Any], key: str, label: str, low: int, high: int,
                  default: int, errors: list[str]) -> int:
    value = raw.get(key)
    if value in (None, ""):
        return default
    try:
        number = int(value)
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        if not low <= number <= high:
            raise ValueError
        return number
    except (TypeError, ValueError):
        errors.append(f"{label} must be a whole number from {low} to {high}.")
        return default


@dataclass(frozen=True)
class Settings:
    """Advanced settings. Defaults match the GUI's empty fields."""

    timeout_s: int = 30            # per visit / request
    time_limit_s: int = 0          # per URL; 0 = automatic
    expect_status: str = ""        # "" = the mode's default (200 / 2xx)
    budget_p95_ms: int = 0         # 0 = no budget
    viewport_width: int = 1440     # browser only
    viewport_height: int = 900
    locale: str = ""               # browser only; "" = Chrome default
    screenshots: bool = True       # browser only

    # (attribute, API name, label, min, max) for the numeric settings.
    NUMBERS = (
        ("timeout_s", "timeoutSec", "Timeout", 1, 300),
        ("time_limit_s", "timeLimitSec", "Time limit", 0, 7200),
        ("budget_p95_ms", "budgetP95Ms", "Budget", 0, 600000),
        ("viewport_width", "viewportWidth", "Screen width", 320, 3840),
        ("viewport_height", "viewportHeight", "Screen height", 320, 2160),
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Settings":
        raw = raw if isinstance(raw, Mapping) else {}
        errors: list[str] = []
        defaults = cls()
        values: dict[str, Any] = {}
        for attr, key, label, low, high in cls.NUMBERS:
            values[attr] = _int_in_range(raw, key, label, low, high, getattr(defaults, attr), errors)
        if 0 < values["time_limit_s"] < 10:
            errors.append("Time limit must be 0 (automatic) or at least 10 seconds.")

        status = re.sub(r"\s+", "", str(raw.get("expectStatus") or "")).lower()
        if status and not _STATUS.match(status):
            errors.append("Expected status must look like 200, 2xx or 200,301.")
        locale = str(raw.get("locale") or "").strip()
        if locale and not _LOCALE.match(locale):
            errors.append("Locale must look like hy-AM or en-US.")
        screenshots = raw["screenshots"] is True if "screenshots" in raw else defaults.screenshots

        if errors:
            raise ValidationError(errors)
        return cls(expect_status=status, locale=locale, screenshots=screenshots, **values)

    def to_dict(self) -> dict[str, Any]:
        """API/GUI form (camelCase)."""
        data = {key: getattr(self, attr) for attr, key, *_ in self.NUMBERS}
        data.update(expectStatus=self.expect_status, locale=self.locale, screenshots=self.screenshots)
        return data

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Defaults and ranges for the GUI (served by GET /api/config)."""
        defaults = cls()
        return {
            "defaults": defaults.to_dict(),
            "ranges": {key: [low, high] for _, key, _, low, high in cls.NUMBERS},
        }


@dataclass(frozen=True)
class ApiRequest:
    """What to send in API mode."""

    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ApiRequest":
        errors: list[str] = []
        method = str(raw.get("method") or "GET").upper()
        if method not in HTTP_METHODS:
            errors.append(f"Method must be one of {', '.join(HTTP_METHODS)}.")
        headers = cls._parse_headers(raw.get("headers"), errors)
        body = raw.get("body")
        body = body if isinstance(body, str) and body != "" else None
        if errors:
            raise ValidationError(errors)
        return cls(method=method, headers=headers, body=body)

    @staticmethod
    def _parse_headers(value: Any, errors: list[str]) -> dict[str, str]:
        """Headers as a dict, a JSON object string, or "Name: value" lines."""
        if value in (None, ""):
            return {}
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{"):
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    errors.append("Headers must be a JSON object or lines like 'Name: value'.")
                    return {}
            else:
                parsed: dict[str, str] = {}
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    if ":" not in line:
                        errors.append(f"Header line {line.strip()!r} needs 'Name: value'.")
                        continue
                    name, header_value = line.split(":", 1)
                    parsed[name.strip()] = header_value.strip()
                return parsed
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            errors.append("Headers must be name/value text pairs.")
            return {}
        return dict(value)


@dataclass(frozen=True)
class BurstPlan:
    """Burst test: N separate users open the page at the same moment, repeated in rounds.

    Each step (number of users) runs ``rounds`` times with a pause in between. Before
    the first burst the site is checked once; after every burst it is watched for up
    to ``recovery_s`` seconds to see whether it still answers ("did it die?").
    """

    steps: tuple[int, ...] = (30,)
    rounds: int = 3
    pause_s: int = 30
    recovery_s: int = 60
    slow_ms: int = 3000          # a burst whose first-byte p95 (server time) is above this counts as slow
    full_page: bool = True       # also download the page's own scripts, styles and images

    MAX_USERS = 1000
    MAX_STEPS = 10
    NUMBERS = (
        ("rounds", "rounds", "Rounds", 1, 10),
        ("pause_s", "pauseSec", "Pause between bursts", 5, 600),
        ("recovery_s", "recoverySec", "Recovery check", 10, 600),
        ("slow_ms", "slowMs", "Slow above", 100, 120000),
    )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BurstPlan":
        errors: list[str] = []
        defaults = cls()
        steps = cls._parse_steps(raw.get("burstUsers"), errors) or defaults.steps
        values = {attr: _int_in_range(raw, key, label, low, high, getattr(defaults, attr), errors)
                  for attr, key, label, low, high in cls.NUMBERS}
        full_page = raw["fullPage"] is True if "fullPage" in raw else defaults.full_page
        if errors:
            raise ValidationError(errors)
        return cls(steps=steps, full_page=full_page, **values)

    @classmethod
    def _parse_steps(cls, value: Any, errors: list[str]) -> tuple[int, ...]:
        if value in (None, ""):
            return ()
        parts = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;]+", str(value).strip())
        try:
            steps = tuple(int(p) for p in parts if str(p).strip())
        except (TypeError, ValueError):
            errors.append("Users per burst must be numbers like 30 or 10, 20, 30.")
            return ()
        if not steps or len(steps) > cls.MAX_STEPS or not all(1 <= s <= cls.MAX_USERS for s in steps):
            errors.append(f"Users per burst: 1 to {cls.MAX_STEPS} numbers, each 1–{cls.MAX_USERS} (e.g. 10, 20, 30).")
            return ()
        return tuple(sorted(set(steps)))

    @property
    def total_requests(self) -> int:
        return sum(self.steps) * self.rounds

    def to_dict(self) -> dict[str, Any]:
        return {"burstUsers": list(self.steps), "fullPage": self.full_page,
                **{key: getattr(self, attr) for attr, key, *_ in self.NUMBERS}}

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {"defaults": cls().to_dict(), "ranges": {key: [low, high] for _, key, _, low, high in cls.NUMBERS},
                "maxUsers": cls.MAX_USERS}


@dataclass(frozen=True)
class RunConfig:
    """One run: one URL, or two to compare, with the same users and visits."""

    urls: tuple[str, ...]
    vus: int
    iterations: int
    mode_name: str = "browser"
    # Browser mode only. None: every user is a real browser. Fewer than vus: hybrid mode,
    # ``browsers`` real browsers measure while the other users create load with HTTP requests.
    browsers: int | None = None
    burst: BurstPlan | None = None      # burst mode only
    wait_until: str = "load"
    request: ApiRequest = field(default_factory=ApiRequest)
    settings: Settings = field(default_factory=Settings)
    graceful_stop_s: int = GRACEFUL_STOP_S

    @property
    def mode(self) -> TestMode:
        return get_mode(self.mode_name)

    @property
    def measuring_users(self) -> int:
        """Users that run the measured visits (real browsers in browser mode)."""
        return self.browsers or self.vus

    @property
    def load_users(self) -> int:
        """Hybrid mode: users that only create load with plain HTTP requests."""
        return self.vus - self.measuring_users if self.mode.needs_browser else 0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RunConfig":
        """Build from GUI/API input (camelCase). Raises ValidationError with all problems."""
        if not isinstance(raw, Mapping):
            raise ValidationError(["Invalid request."])
        errors: list[str] = []
        mode_name = ("burst" if raw.get("mode") == "burst"
                     else "api" if raw.get("apiOnly") is True or raw.get("mode") == "api" else "browser")
        mode = MODES[mode_name]
        if mode_name == "burst":
            return cls._burst_from_dict(raw, mode, errors)

        urls: list[str] = []
        for key in ("url", "compareUrl"):
            value = str(raw.get(key) or "").strip()
            if not value:
                if key == "url":
                    errors.append("URL is required.")
                continue
            if not _URL.match(value):
                errors.append(f"{value!r} is not an http(s) URL.")
            urls.append(value)

        vus = _int_in_range(raw, "vus", "Parallel users", 1, mode.limits.max_vus, 0, errors) \
            if raw.get("vus") not in (None, "") else _missing("Parallel users", errors)
        iterations = _int_in_range(raw, "iterations", f"Total {mode.noun}", 1, mode.limits.max_iterations, 0, errors) \
            if raw.get("iterations") not in (None, "") else _missing(f"Total {mode.noun}", errors)
        browsers = None
        if mode.needs_browser and raw.get("browsers") not in (None, "", 0, "0"):
            browsers = _int_in_range(raw, "browsers", "Real browsers", 1, max(vus, 1), 0, errors) or None
        measuring = browsers or vus
        if measuring and iterations and measuring > iterations:
            who = "Real browsers" if browsers else "Parallel users"
            errors.append(
                f"{who} ({measuring}) can't be more than total {mode.noun} ({iterations}): "
                f"each one needs at least one {mode.noun_singular}."
            )

        wait_until = str(raw.get("waitUntil") or "load")
        if wait_until not in WAIT_MODES:
            errors.append("Wait until must be load or networkidle.")

        request = ApiRequest()
        if mode_name == "api":
            request = _collect(errors, ApiRequest.from_dict, raw) or request
        settings = _collect(errors, Settings.from_dict, raw.get("settings")) or Settings()

        if errors:
            raise ValidationError(errors)
        if browsers == vus:
            browsers = None
        return cls(urls=tuple(urls), vus=vus, iterations=iterations, mode_name=mode_name,
                   browsers=browsers, wait_until=wait_until, request=request, settings=settings)

    @classmethod
    def _burst_from_dict(cls, raw: Mapping[str, Any], mode: TestMode, errors: list[str]) -> "RunConfig":
        urls = cls._urls(raw, errors)
        plan = _collect(errors, BurstPlan.from_dict, raw) or BurstPlan()
        request = _collect(errors, ApiRequest.from_dict, {"headers": raw.get("headers")}) or ApiRequest()
        settings = _collect(errors, Settings.from_dict, raw.get("settings")) or Settings()
        if errors:
            raise ValidationError(errors)
        return cls(urls=tuple(urls), vus=max(plan.steps), iterations=plan.total_requests, mode_name="burst",
                   request=request, settings=settings, burst=plan)

    @staticmethod
    def _urls(raw: Mapping[str, Any], errors: list[str]) -> list[str]:
        urls: list[str] = []
        for key in ("url", "compareUrl"):
            value = str(raw.get(key) or "").strip()
            if not value:
                if key == "url":
                    errors.append("URL is required.")
                continue
            if not _URL.match(value):
                errors.append(f"{value!r} is not an http(s) URL.")
            urls.append(value)
        return urls

    def warnings(self) -> list[str]:
        """Allowed but risky choices: above the recommended users or visits."""
        limits, noun = self.mode.limits, self.mode.noun
        notes = []
        if self.burst:
            if max(self.burst.steps) > limits.recommended_vus:
                notes.append(f"Bursts of up to {max(self.burst.steps)} users: {limits.load_note}.")
            return notes
        if self.mode.needs_browser:
            if self.measuring_users > limits.recommended_vus:
                notes.append(f"{self.measuring_users} browser users is more than the recommended "
                             f"{limits.recommended_vus} for this computer: {limits.load_note}.")
            api_limits = get_mode("api").limits
            if self.load_users > api_limits.recommended_vus:
                notes.append(f"{self.load_users} load users: {api_limits.load_note}.")
        elif self.vus > limits.recommended_vus:
            notes.append(f"{self.vus} parallel users is more than the recommended {limits.recommended_vus} "
                         f"for {self.mode.kind} tests: {limits.load_note}.")
        if self.iterations > limits.recommended_iterations:
            notes.append(f"{self.iterations} {noun} per URL is more than the recommended "
                         f"{limits.recommended_iterations}: the run will take a long time.")
        return notes

    def expected_status(self) -> str:
        return self.settings.expect_status or self.mode.default_expect_status

    def time_limit_s(self) -> int:
        """Run time limit per URL; visits still running after it count as hung."""
        if self.settings.time_limit_s:
            return self.settings.time_limit_s
        rounds = -(-self.iterations // self.measuring_users)  # visits per user, rounded up
        return rounds * (self.settings.timeout_s + self.mode.overhead_per_visit_s) + 30

    def to_dict(self) -> dict[str, Any]:
        return {
            "urls": list(self.urls),
            "vus": self.vus,
            "browsers": self.browsers,
            "loadUsers": self.load_users,
            "iterations": self.iterations,
            "mode": self.mode_name,
            "waitUntil": self.wait_until,
            "request": asdict(self.request) if self.mode_name == "api" else None,
            "burst": self.burst.to_dict() if self.burst else None,
            "settings": self.settings.to_dict(),
            "warnings": self.warnings(),
        }


def _missing(label: str, errors: list[str]) -> int:
    errors.append(f"{label} is required.")
    return 0


def _collect(errors: list[str], factory, raw):
    """Call a from_dict factory, adding its errors to ``errors`` instead of raising."""
    try:
        return factory(raw)
    except ValidationError as error:
        errors.extend(error.errors)
        return None
