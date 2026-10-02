#!/usr/bin/env python3
"""Sign in to a platform once, in a visible browser -- the `Scraper: Log In to X` task.

The CLI contract devkit's dispatcher calls (`devkit_project.py --project social-scraper
scraper-login x`). It runs `uv run social-scraper login <platform>` from the repo root,
because `Settings.data_dir` is relative: from any other cwd the session would be saved
somewhere the scraper never looks, instead of in `data/browser-profiles/<platform>/`.

Which platforms exist is the CLI's to say, so an unknown one is rejected there.

Usage:
    python scripts/login.py x
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def login_command(uv: str, platform: str) -> list[str]:
    """The argv that signs in to `platform`, run through the project's own environment."""
    return [uv, "run", "social-scraper", "login", platform]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("platform", help="the platform to sign in to, e.g. x")
    args = parser.parse_args(argv)
    uv = shutil.which("uv")
    if uv is None:
        print("uv is not on PATH; install it to run social-scraper.", file=sys.stderr)
        return 2
    return subprocess.run(login_command(uv, args.platform), cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
