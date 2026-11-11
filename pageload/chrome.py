"""One shared headless Chrome for a browser run (context manager).

k6 normally launches a new Chrome for every visit; with parallel users one of
those launches sometimes hangs forever. Instead we start one Chrome, put the
:class:`~pageload.relay.ConnectionRelay` in front of it and let k6 connect
through ``K6_BROWSER_WS_URL``. Every visit still gets its own fresh, isolated
browser context.

    with SharedChrome() as chrome:
        if chrome.ws_url: ...   # None if Chrome could not start; k6 then launches its own

Chrome, the relay and the temporary profile are always cleaned up on exit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .process import kill_tree
from .relay import ConnectionRelay

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def find_chrome() -> str | None:
    """CHROME_PATH, the usual macOS locations, or a chrome/chromium command."""
    configured = os.environ.get("CHROME_PATH")
    candidates = [configured] if configured else []
    candidates += list(CHROME_CANDIDATES) + [shutil.which(cmd) for cmd in CHROME_COMMANDS]
    return next((c for c in candidates if c and os.access(c, os.X_OK)), None)


def extra_chrome_flags() -> list[str]:
    """Extra Chrome flags from CHROME_FLAGS, e.g. "--no-sandbox --disable-dev-shm-usage" in Docker."""
    return os.environ.get("CHROME_FLAGS", "").split()


class SharedChrome:
    def __init__(self, chrome_path: str | None = None, startup_timeout: float = 10):
        self.chrome_path = chrome_path or find_chrome()
        self.startup_timeout = startup_timeout
        self.ws_url: str | None = None
        self.message: str | None = None   # why there is no shared Chrome, if any
        self._process: subprocess.Popen | None = None
        self._profile: str | None = None
        self._relay: ConnectionRelay | None = None

    def __enter__(self) -> "SharedChrome":
        try:
            self._start()
        except Exception as error:  # noqa: BLE001 - k6 can still launch its own browser
            self.message = f"Could not start shared Chrome ({error}); k6 will launch its own browser."
            self.close()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _start(self) -> None:
        if not self.chrome_path:
            raise RuntimeError("Chrome not found")
        self._profile = tempfile.mkdtemp(prefix="pageload-chrome-")
        # Port 0: Chrome picks a free port and writes it to DevToolsActivePort.
        self._process = subprocess.Popen(
            [self.chrome_path, "--headless=new", "--remote-debugging-port=0",
             f"--user-data-dir={self._profile}", "--no-first-run", "--no-default-browser-check",
             "--disable-background-networking", "--disable-component-update",
             *extra_chrome_flags(), "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        port, ws_path = self._wait_for_devtools()
        self._relay = ConnectionRelay(port)
        relay_port = self._relay.start()
        self.ws_url = f"ws://127.0.0.1:{relay_port}{ws_path}"

    def _wait_for_devtools(self) -> tuple[int, str]:
        """The port file appears before Chrome accepts connections, so also
        wait until it answers /json/version."""
        port_file = Path(self._profile) / "DevToolsActivePort"
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("Chrome exited during startup")
            lines = port_file.read_text().splitlines() if port_file.exists() else []
            if len(lines) >= 2 and lines[0].isdigit():
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{lines[0]}/json/version", timeout=1) as answer:
                        if b"webSocketDebuggerUrl" in answer.read():
                            return int(lines[0]), lines[1]
                except OSError:
                    pass
            time.sleep(0.1)
        raise RuntimeError("Chrome did not become ready in time")

    def close(self) -> None:
        if self._relay:
            self._relay.stop()
            self._relay = None
        if self._process:
            kill_tree(self._process)
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(5)
            self._process = None
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None
        self.ws_url = None
