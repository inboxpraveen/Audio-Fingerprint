#!/usr/bin/env python3
"""Convenience launcher kept for backwards compatibility.

    python run.py                       # same as: audiofp serve
    python run.py --port 8080
    python run.py --env production      # same as: audiofp serve --profile production

Prefer the CLI: ``audiofp serve`` (after ``pip install -e .``) or ``python -m fingerprint serve``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fingerprint.cli import main


def _translate(argv: list[str]) -> list[str]:
    out = ["serve"]
    it = iter(argv)
    for arg in it:
        if arg == "--env":
            out += ["--profile", next(it, "development")]
        elif arg.startswith("--env="):
            out += ["--profile", arg.split("=", 1)[1]]
        else:
            out.append(arg)
    return out


if __name__ == "__main__":
    sys.exit(main(_translate(sys.argv[1:])))
