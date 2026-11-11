PYTHON ?= python3

.PHONY: help run api test install clean docker-build docker-up docker-down docker-logs

help:            ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

run:             ## Start the GUI on http://127.0.0.1:8000
	$(PYTHON) app.py

api:             ## Start the HTTP API only (no GUI)
	$(PYTHON) app.py --api-only

test:            ## Run the test suite (no k6 or Chrome needed)
	$(PYTHON) -m unittest discover -s tests -t . -v

install:         ## Install the "pageload" command for this user
	$(PYTHON) -m pip install --user .

docker-build:    ## Build the Docker image (Python + k6 + Chromium)
	docker compose build

docker-up:       ## Start the GUI in Docker on http://localhost:8000
	docker compose up -d --build
	@echo "Page Load Compare: http://localhost:8000"

docker-down:     ## Stop the Docker container
	docker compose down

docker-logs:     ## Follow the Docker container's output
	docker compose logs -f

clean:           ## Remove caches (keeps reports/)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf build *.egg-info
