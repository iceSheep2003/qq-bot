"""Run the TDD regression set instead of the whole suite.

The full suite is the release gate: it answers "is everything still right?".
This answers "did I just break the part I am working on?" and is meant to be run
after every edit. The set lives in ``tests/regression.txt`` so it is reviewable
in a diff rather than buried in a shell alias.

Usage:
    .venv/bin/python -m tools.quality.fast            # run the set
    .venv/bin/python -m tools.quality.fast --list     # show what is in it
    .venv/bin/python -m tools.quality.fast --check    # fail if a listed file is gone
    .venv/bin/python -m tools.quality.fast --junit=.quality/regression-set.xml
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/regression.txt"


def load(manifest: Path = MANIFEST) -> list[str]:
    """Paths from the manifest, comments and blank lines dropped."""
    if not manifest.is_file():
        raise SystemExit(f"missing regression manifest: {manifest}")
    entries = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        entries.append(entry)
    if not entries:
        raise SystemExit("regression manifest is empty")
    return entries


def missing(entries) -> list[str]:
    return [entry for entry in entries if not (ROOT / entry).exists()]


def main(argv) -> int:
    entries = load()
    absent = missing(entries)
    if absent:
        print("regression set points at files that no longer exist:", file=sys.stderr)
        for entry in absent:
            print(f"  {entry}", file=sys.stderr)
        print("update tests/regression.txt or restore the file", file=sys.stderr)
        return 2
    if "--list" in argv:
        for entry in entries:
            print(entry)
        print(f"\n{len(entries)} files")
        return 0
    if "--check" in argv:
        print(f"regression set: {len(entries)} files, all present")
        return 0
    junit = next((item.split("=", 1)[1] for item in argv if item.startswith("--junit=")), None)
    command = [sys.executable, "-m", "pytest", *entries, "-q", "-p", "no:cacheprovider"]
    if junit:
        command.append(f"--junitxml={junit}")
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, check=False)
    elapsed = time.monotonic() - started
    print(f"\nregression set finished in {elapsed:.1f}s ({len(entries)} files)")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
