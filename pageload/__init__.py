"""Page Load Compare: measure and compare website load and API response times with k6.

Two test modes share one workflow (same users, same number of visits per URL):

* browser — a real Chrome loads the page (TTFB, FCP, LCP, load, fully loaded)
* api     — plain HTTP requests, no browser (connect, TLS, TTFB, receive, total)

Entry points: the GUI/API server and the command line in :mod:`pageload.cli`.
"""

__version__ = "1.0.0"
