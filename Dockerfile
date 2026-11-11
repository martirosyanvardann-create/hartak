# Page Load Compare: Python app + k6 + Chromium in one image.
#   docker compose up            -> GUI on http://localhost:8000
ARG K6_VERSION=2.3.0

FROM grafana/k6:${K6_VERSION} AS k6

FROM python:3.12-slim

# Chromium for browser mode (plus fonts so pages render like on a desktop).
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-noto-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=k6 /usr/bin/k6 /usr/local/bin/k6

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pageload ./pageload
RUN pip install --no-cache-dir . && rm -rf /app/build

# Run as a normal user; Chrome inside a container still needs --no-sandbox.
RUN useradd --create-home --uid 1000 pageload && mkdir -p /data/reports && chown -R pageload /data
USER pageload

ENV PAGELOAD_HOST=0.0.0.0 \
    PAGELOAD_PORT=8000 \
    PAGELOAD_REPORTS_DIR=/data/reports \
    CHROME_PATH=/usr/bin/chromium \
    CHROME_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu" \
    K6_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    K6_BROWSER_ARGS="no-sandbox,disable-dev-shm-usage,disable-gpu" \
    PYTHONUNBUFFERED=1

VOLUME /data/reports
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

ENTRYPOINT ["pageload"]
CMD ["serve", "--no-browser"]
