"""Run actual Python quality tools for the Code Quality Gate contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.quality.junit_report import normalize_junit

ROOT = Path(__file__).resolve().parents[2]
NEW_FILES = [
    "src/qqbot/agent/conversation/channels.py",
    "src/qqbot/agent/skills/audience.py",
    "src/qqbot/agent/cognition/routes.py",
]
CODE_FILES = NEW_FILES + [
    'src/qqbot/agent/conversation/delivery.py', 'src/qqbot/agent/service.py', 'src/qqbot/agent/observation.py', 'src/qqbot/contracts/ports.py', 'tests/agent/test_delivery_boundary.py',
    "src/qqbot/agent/conversation/maintenance.py", "tests/agent/test_conversation_maintenance.py",
    "src/qqbot/agent/observation.py", "tests/agent/test_observation.py",
    "src/qqbot/agent/cognition/__init__.py", "src/qqbot/agent/skills/catalog.py",
    "src/qqbot/agent/skills/host.py", "src/qqbot/agent/orchestrator.py", "src/qqbot/agent/service.py",
    "src/qqbot/actions/tools/kernel.py", "src/qqbot/bootstrap/app.py", "src/qqbot/bootstrap/config.py",
    "src/qqbot/agent/cognition/experience/runtime.py", "src/qqbot/contracts/extension_inputs.py",
    "src/qqbot/infrastructure/tool_handlers.py", "src/qqbot/contracts/ports.py", "tools/quality/junit_report.py",
]


def run(args: list[str]) -> int:
    return subprocess.run(args, cwd=ROOT, check=False).returncode


def coverage_report(files=NEW_FILES, prefix="new", metrics_name="coverage-metrics") -> int:
    result = run([
        sys.executable, "-m", "coverage", "json", "--include=" + ",".join(files),
        "-o", f".quality/{prefix}-coverage.json",
    ])
    if result:
        return result
    report = json.loads((ROOT / f".quality/{prefix}-coverage.json").read_text())
    totals = report["totals"]
    statements, branches = totals["num_statements"], totals["num_branches"]
    if not statements or not branches or set(report["files"]) != set(files):
        raise ValueError("Missing source or coverage denominators")
    metrics = {
        "lineCoverage": 100 * totals["covered_lines"] / statements,
        "branchCoverage": 100 * totals["covered_branches"] / branches,
        "statements": statements,
        "branches": branches,
    }
    (ROOT / f".quality/{metrics_name}.json").write_text(json.dumps(metrics))
    print(json.dumps(metrics))
    return 0


def main() -> int:
    (ROOT / ".quality").mkdir(exist_ok=True)
    commands = {
        "acceptance": [sys.executable, "-m", "pytest", "tests/agent/test_scene_refactor.py",
                       "-q", "--junitxml=.quality/acceptance.xml"],
        "regression": [sys.executable, "-m", "coverage", "run", "--branch",
                       "--data-file=.coverage", "-m", "pytest", "tests", "-q",
                       "--junitxml=.quality/regression.xml"],
        "lint": [str(Path(sys.executable).parent / "ruff"), "check", "--select",
                 "E9,F", *CODE_FILES, "tests/agent/test_scene_refactor.py", __file__],
        "architecture": [str(Path(sys.executable).parent / "lint-imports"),
                         "--config", ".importlinter", "--no-cache"],
    }
    if len(sys.argv) != 2:
        raise ValueError("One check name is required")
    if sys.argv[1] == "coverage":
        return coverage_report()
    if sys.argv[1] == "observation-coverage":
        return coverage_report(["src/qqbot/agent/observation.py"], "observation", "observation-coverage-metrics")
    if sys.argv[1] == "maintenance-coverage":
        return coverage_report(["src/qqbot/agent/conversation/maintenance.py"], "maintenance", "maintenance-coverage-metrics")
    if sys.argv[1] == 'delivery-boundary-coverage':
        return coverage_report(['src/qqbot/agent/conversation/delivery.py'], 'delivery-boundary', 'delivery-boundary-coverage-metrics')
    result = run(commands[sys.argv[1]])
    if sys.argv[1] == "regression":
        normalize_junit(ROOT / ".quality/regression.xml")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
