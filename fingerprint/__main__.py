"""Run the audiofp CLI as ``python -m fingerprint``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
