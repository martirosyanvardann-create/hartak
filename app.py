#!/usr/bin/env python3
"""Start Page Load Compare from a source checkout: ``python3 app.py [command]``.

Same as ``python3 -m pageload`` or the installed ``pageload`` command;
see pageload/cli.py and README.md.
"""

import sys

from pageload.cli import main

if __name__ == "__main__":
    sys.exit(main())
