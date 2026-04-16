#!/usr/bin/env python3
"""AudioFP — entry point.

Usage:
    python run.py                          # development, port 5000
    python run.py --port 8080
    python run.py --env production --port 80
"""

import os
import sys
import argparse

# Make sure the project root is on the path regardless of where the user
# launches the script from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fingerprint.api.app import create_app  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="AudioFP — Audio Intelligence Platform")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default: 5000)")
    parser.add_argument(
        "--env",
        default="development",
        choices=["development", "production"],
        help="Configuration profile (default: development)",
    )
    args = parser.parse_args()

    app = create_app(args.env)

    print(f"\n  AudioFP server running")
    print(f"  Open http://localhost:{args.port} in your browser\n")

    app.run(
        host=args.host,
        port=args.port,
        debug=(args.env == "development"),
        threaded=True,
    )


if __name__ == "__main__":
    main()
