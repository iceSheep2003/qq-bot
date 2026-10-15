"""Run actual Python quality tools for the frozen VerityGate contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from junit_report import normalize_junit

ROOT = Path(__file__).resolve().parents[1]
NEW_FILES = [
    "qunbot/conversation/channels.py",
    "qunbot/skills/audience.py",
    "qunbot/cognition/routes.py",
]
CODE_FILES = NEW_FILES + [
    "qunbot/cognition/__init__.py", "qunbot/runtime/skills.py",
    "qunbot/skills/host.py", "qunbot/runtime/agent.py", "qunbot/runtime/service.py",
    "qunbot/harness/tools.py", "qunbot/app.py", "qunbot/config.py",
    "qunbot/experience/runtime.py", "qunbot/contracts/extension_inputs.py",
    "qunbot/bootstrap/tool_handlers.py", "qunbot/ports.py", "scripts/junit_report.py",
]


def run(args: list[str]) -> int:
    return subprocess.run(args, cwd=ROOT, check=False).returncode


def coverage_report() -> int:
    result = run([
        sys.executable, "-m", "coverage", "json", "--include=" + ",".join(NEW_FILES),
        "-o", ".quality/new-coverage.json",
    ])
    if result:
        return result
    report = json.loads((ROOT / ".quality/new-coverage.json").read_text())
    totals = report["totals"]
    statements, branches = totals["num_statements"], totals["num_branches"]
    if not statements or not branches or set(report["files"]) != set(NEW_FILES):
        raise ValueError("Missing source or coverage denominators")
    metrics = {
        "lineCoverage": 100 * totals["covered_lines"] / statements,
        "branchCoverage": 100 * totals["covered_branches"] / branches,
        "statements": statements,
        "branches": branches,
    }
    (ROOT / ".quality/coverage-metrics.json").write_text(json.dumps(metrics))
    print(json.dumps(metrics))
    return 0


def main() -> int:
    (ROOT / ".quality").mkdir(exist_ok=True)
    commands = {
        "acceptance": [sys.executable, "-m", "pytest", "tests/test_scene_refactor.py",
                       "-q", "--junitxml=.quality/acceptance.xml"],
        "regression": [sys.executable, "-m", "coverage", "run", "--branch",
                       "--data-file=.coverage", "-m", "pytest", "tests", "-q",
                       "--junitxml=.quality/regression.xml"],
        "lint": [str(Path(sys.executable).parent / "ruff"), "check", "--select",
                 "E9,F", *CODE_FILES, "tests/test_scene_refactor.py", __file__],
        "architecture": [str(Path(sys.executable).parent / "lint-imports"),
                         "--config", ".importlinter", "--no-cache"],
    }
    if len(sys.argv) != 2:
        raise ValueError("One check name is required")
    if sys.argv[1] == "coverage":
        return coverage_report()
    result = run(commands[sys.argv[1]])
    if sys.argv[1] == "regression":
        normalize_junit(ROOT / ".quality/regression.xml")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
