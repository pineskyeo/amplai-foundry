"""`python -m demo_app` entry point."""

from __future__ import annotations

import sys

from demo_app.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin, sys.stdout, sys.stderr))
