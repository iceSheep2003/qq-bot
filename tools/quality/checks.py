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
CONSOLE_FILES = [
    "src/qqbot/interfaces/webui/logbuffer.py",
    "src/qqbot/interfaces/webui/monitor_query.py",
    "src/qqbot/interfaces/webui/route_query.py",
    "src/qqbot/interfaces/webui/skill_admin.py",
    "src/qqbot/interfaces/webui/tools_query.py",
]
CODE_FILES = NEW_FILES + CONSOLE_FILES + [
    'src/qqbot/infrastructure/storage/emotion.py', 'src/qqbot/agent/identity/emotion/system.py', 'src/qqbot/agent/identity/emotion/evaluator.py', 'src/qqbot/agent/identity/emotion/__init__.py', 'src/qqbot/features/mood/composition.py', 'src/qqbot/features/mood/register.py', 'src/qqbot/agent/memory/service.py', 'src/qqbot/bootstrap/app.py', 'src/qqbot/agent/service.py', 'src/qqbot/contracts/models.py', 'src/qqbot/contracts/ports.py', 'src/qqbot/actions/scheduling/registry.py', 'tests/features/test_slang.py', 'tests/features/test_reply_policy.py', 'tests/agent/test_conversation_maintenance.py', 'tests/agent/test_service.py', 'tests/agent/test_observation.py', 'tests/agent/test_core.py', 'tests/agent/test_identity_guard.py', 'tests/agent/test_inbound_media.py', 'tests/agent/test_relationships.py', 'tests/agent/test_delivery_boundary.py', 'tests/agent/test_emotion.py', 'tests/agent/test_memory.py', 'src/qqbot/agent/memory/ranking.py', 'src/qqbot/infrastructure/storage/memory.py', 'src/qqbot/agent/cognition/experience/retrieval.py', 'tests/interfaces/test_webui.py', 'tests/agent/test_embedding_retirement.py',
    'src/qqbot/infrastructure/storage/emotion.py', 'src/qqbot/agent/identity/emotion/system.py', 'src/qqbot/agent/identity/emotion/evaluator.py', 'src/qqbot/agent/identity/emotion/__init__.py', 'src/qqbot/features/mood/composition.py', 'src/qqbot/features/mood/register.py', 'src/qqbot/agent/memory/service.py', 'src/qqbot/bootstrap/app.py', 'src/qqbot/agent/service.py', 'src/qqbot/contracts/models.py', 'src/qqbot/contracts/ports.py', 'src/qqbot/actions/scheduling/registry.py', 'tests/features/test_slang.py', 'tests/features/test_reply_policy.py', 'tests/agent/test_conversation_maintenance.py', 'tests/agent/test_service.py', 'tests/agent/test_observation.py', 'tests/agent/test_core.py', 'tests/agent/test_identity_guard.py', 'tests/agent/test_inbound_media.py', 'tests/agent/test_relationships.py', 'tests/agent/test_delivery_boundary.py', 'tests/agent/test_emotion.py', 'tests/agent/test_memory.py', 'tests/quality/test_core_io_boundaries.py',
    'src/qqbot/bootstrap/runtime.py', 'src/qqbot/bootstrap/app.py', 'src/qqbot/interfaces/qq/router.py', 'src/qqbot/interfaces/qq/__init__.py', 'src/qqbot/features/loader.py', 'tests/features/test_extensions.py', 'tests/bootstrap/test_assembly.py', 'tests/bootstrap/test_runtime_boundaries.py',
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
    reports = ROOT / ".quality"
    if not reports.is_dir():
        reports.mkdir(parents=True, exist_ok=True)
    commands = {
        "acceptance": [sys.executable, "-m", "pytest", "tests/agent/test_scene_refactor.py",
                       "-q", "--junitxml=.quality/acceptance.xml"],
        "regression": [sys.executable, "-m", "coverage", "run", "--branch",
                       "--data-file=.coverage", "-m", "pytest", "tests", "-q", "-W", "error::RuntimeWarning",
                       "-W", "error::pytest.PytestUnraisableExceptionWarning",
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
    if sys.argv[1] == "console-coverage":
        return coverage_report(CONSOLE_FILES, "console", "console-coverage-metrics")
    if sys.argv[1] == "observation-coverage":
        return coverage_report(["src/qqbot/agent/observation.py"], "observation", "observation-coverage-metrics")
    if sys.argv[1] == "maintenance-coverage":
        return coverage_report(["src/qqbot/agent/conversation/maintenance.py"], "maintenance", "maintenance-coverage-metrics")
    if sys.argv[1] == 'delivery-boundary-coverage':
        return coverage_report(['src/qqbot/agent/conversation/delivery.py'], 'delivery-boundary', 'delivery-boundary-coverage-metrics')
    if sys.argv[1] == 'runtime-boundary-coverage':
        return coverage_report(['src/qqbot/bootstrap/runtime.py', 'src/qqbot/interfaces/qq/router.py'], 'runtime-boundary', 'runtime-boundary-coverage-metrics')
    result = run(commands[sys.argv[1]])
    if sys.argv[1] == "regression":
        normalize_junit(ROOT / ".quality/regression.xml")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
