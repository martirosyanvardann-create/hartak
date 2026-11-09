#!/usr/bin/env bash
# Runs loadtest.js and builds an HTML report with one row per visit.
#
# Usage:
#   ./run-report.sh                                    # smoke, https://hartak.am/
#   ./run-report.sh -e PROFILE=baseline                # 30 visits
#   ./run-report.sh -e BASE_URL=https://example.com/   # any website
#   ./run-report.sh -e WAIT_UNTIL=networkidle          # wait for network to go quiet
#   ./run-report.sh -e PROFILE=load -e VUS=3 -e DURATION=1m
#
# Any k6 options (-e ...) are passed through.
# REPORT_OUT=<folder> sets the output folder (used by app.py).
# SCRIPT=apitest.js runs the HTTP/API test instead (no browser), e.g.
#   SCRIPT=apitest.js ./run-report.sh -e BASE_URL=https://hartak.am/ -e VUS=20 -e ITERATIONS=200
#
# Shared browser: k6 normally launches a new Chrome for every visit, and with
# parallel users one of those launches sometimes hangs forever. This script
# starts ONE headless Chrome and lets k6 connect to it (K6_BROWSER_WS_URL).
# Every visit still gets a fresh, isolated browser context.
# Set SHARED_BROWSER=0 to let k6 launch Chrome itself; CHROME_PATH to pick Chrome.
# Output: reports/<timestamp>-<host>/report.html

set -uo pipefail
cd "$(dirname "$0")"

K6="${K6:-$(command -v k6 || echo /opt/homebrew/bin/k6)}"
# Name the report folder after the host, e.g. 20261109-165000-dev.hartak.am.
URL="https://hartak.am/"
for arg in "$@"; do
  case "$arg" in BASE_URL=*) URL="${arg#BASE_URL=}" ;; esac
done
HOST="$(echo "$URL" | sed -E 's#^[a-zA-Z]+://##; s#[/:?].*$##')"
DIR="${REPORT_OUT:-reports/$(date +%Y%m%d-%H%M%S)-${HOST:-site}}"
mkdir -p "$DIR" screenshots

find_chrome() {
  for candidate in \
    "${CHROME_PATH:-}" \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "$(command -v google-chrome || true)" \
    "$(command -v chromium || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

CHROME_PID=""
CHROME_DATA=""
RELAY_PID=""

stop_chrome() {
  if [ -n "$RELAY_PID" ]; then
    kill "$RELAY_PID" 2>/dev/null
    wait "$RELAY_PID" 2>/dev/null
    RELAY_PID=""
  fi
  if [ -n "$CHROME_PID" ]; then
    kill "$CHROME_PID" 2>/dev/null
    wait "$CHROME_PID" 2>/dev/null
    CHROME_PID=""
  fi
  if [ -n "$CHROME_DATA" ]; then
    rm -rf "$CHROME_DATA" 2>/dev/null
    CHROME_DATA=""
  fi
}
trap stop_chrome EXIT
trap 'stop_chrome; exit 143' INT TERM

SCRIPT="${SCRIPT:-loadtest.js}"
case "$SCRIPT" in
  loadtest.js|apitest.js) ;;
  *) echo "SCRIPT must be loadtest.js or apitest.js" >&2; exit 2 ;;
esac

# The API test sends plain HTTP requests: no Chrome needed.
if [ "$SCRIPT" = "loadtest.js" ] && [ "${SHARED_BROWSER:-1}" = "1" ] && [ -z "${K6_BROWSER_WS_URL:-}" ]; then
  if CHROME="$(find_chrome)"; then
    CHROME_DATA="$(mktemp -d)"
    # Port 0: Chrome picks a free port and writes it to DevToolsActivePort.
    "$CHROME" --headless=new --remote-debugging-port=0 \
      --user-data-dir="$CHROME_DATA" --no-first-run --no-default-browser-check \
      --disable-background-networking --disable-component-update \
      about:blank >/dev/null 2>&1 &
    CHROME_PID=$!

    for _ in $(seq 1 100); do
      [ -s "$CHROME_DATA/DevToolsActivePort" ] && break
      sleep 0.1
    done

    CHROME_READY=0
    if [ -s "$CHROME_DATA/DevToolsActivePort" ]; then
      PORT="$(sed -n 1p "$CHROME_DATA/DevToolsActivePort")"
      WS_PATH="$(sed -n 2p "$CHROME_DATA/DevToolsActivePort")"
      # The port file appears before Chrome accepts connections; with many
      # users connecting at once that caused "broken pipe". Wait until
      # Chrome actually answers.
      for _ in $(seq 1 100); do
        if curl -s --max-time 1 "http://127.0.0.1:${PORT}/json/version" | grep -q webSocketDebuggerUrl; then
          CHROME_READY=1
          break
        fi
        sleep 0.1
      done
    fi

    # Relay in front of Chrome: accepts all users' connections at once and
    # passes them to Chrome as fast as it can take them (see cdp-relay.py).
    RELAY_PORT=""
    if [ "$CHROME_READY" = "1" ]; then
      python3 -u cdp-relay.py "$PORT" > "$CHROME_DATA/relay.port" 2>/dev/null &
      RELAY_PID=$!
      for _ in $(seq 1 50); do
        RELAY_PORT="$(head -n 1 "$CHROME_DATA/relay.port" 2>/dev/null)"
        [ -n "$RELAY_PORT" ] && break
        sleep 0.1
      done
      if [ -z "$RELAY_PORT" ]; then
        echo "Connection relay did not start; connecting to Chrome directly." >&2
        kill "$RELAY_PID" 2>/dev/null
        RELAY_PID=""
        RELAY_PORT="$PORT"
      fi
    fi

    if [ "$CHROME_READY" = "1" ]; then
      export K6_BROWSER_WS_URL="ws://127.0.0.1:${RELAY_PORT}${WS_PATH}"
    else
      echo "Could not start shared Chrome; k6 will launch its own." >&2
      stop_chrome
    fi
  else
    echo "Chrome not found; k6 will launch its own browser." >&2
  fi
fi

"$K6" run -q \
  --log-format raw \
  --console-output "$DIR/console.log" \
  -e REPORT_DIR="$DIR" \
  -e SCREENSHOT_ON_FAIL="${SCREENSHOT_ON_FAIL:-1}" \
  "$@" \
  "$SCRIPT"
K6_STATUS=$?
stop_chrome

# Show errors and other log lines (everything except the visit records).
grep -v '^VISIT ' "$DIR/console.log" >&2 || true

python3 make-report.py "$DIR" || exit 1

exit "$K6_STATUS"
