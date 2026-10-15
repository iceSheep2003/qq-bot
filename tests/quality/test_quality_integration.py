"""Verify the actual released quality package and its project integration."""
import json
import hashlib
import zipfile
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLCHAIN = ROOT / ".code-quality-gate/toolchain"
CLI = TOOLCHAIN / "node_modules/code-quality-gate/src/cli.mjs"


def test_released_package_and_real_cli_are_used():
    declared = json.loads((TOOLCHAIN / "package.json").read_text())
    assert declared["dependencies"] == {"code-quality-gate": "file:vendor/code-quality-gate-0.6.0.tgz"}
    installed = json.loads((CLI.parents[1] / "package.json").read_text())
    assert installed["name"] == "code-quality-gate"
    assert installed["version"] == "0.6.0"
    result = subprocess.run(["node", str(CLI), "version"], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stdout.strip() == "0.6.0"
    with tarfile.open(TOOLCHAIN / "vendor/code-quality-gate-0.6.0.tgz") as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.startswith("package/src/"):
                assert archive.extractfile(member).read() == (CLI.parents[1] / member.name.removeprefix("package/")).read_bytes(), member.name


def test_bundled_skill_and_managed_instructions_are_installed():
    with tarfile.open(TOOLCHAIN / "vendor/code-quality-gate-0.6.0.tgz") as archive:
        official = archive.extractfile("package/skills/code-quality-gate/SKILL.md").read()
    for relative in (".agents/skills/code-quality-gate/SKILL.md", ".claude/skills/code-quality-gate/SKILL.md"):
        path = ROOT / relative
        assert path.is_file(), relative
        assert path.read_bytes() == official
    for relative in ("AGENTS.md", "CLAUDE.md"):
        text = (ROOT / relative).read_text()
        assert "code-quality-gate:start" in text
        assert "veritygate:start" not in text


def test_ci_and_mcp_use_one_current_quality_engine():
    workflow = ROOT / ".github/workflows/code-quality-gate.yml"
    assert workflow.is_file()
    text = workflow.read_text()
    assert ".code-quality-gate/ci-setup.sh" in text
    assert "node_modules/code-quality-gate/src/cli.mjs gate" in text
    assert "setup-python" in text
    assert not (ROOT / ".github/workflows/veritygate.yml").exists()
    settings = json.loads((ROOT / ".mcp.json").read_text())
    assert "code-quality-gate" in settings["mcpServers"]
    assert "veritygate" not in settings["mcpServers"]


def test_old_engine_is_archived_without_a_second_active_tree():
    assert not (ROOT / ".veritygate").exists()
    assert not (ROOT / ".agents/skills/veritygate").exists()
    archive = ROOT / ".code-quality-gate/history/veritygate-0.4.0.zip"
    assert archive.is_file()
    with zipfile.ZipFile(archive) as history:
        evidence = json.loads(history.read(".veritygate/evidence/QQBOT-IDENTITY-005-R3.json"))
        assert evidence["attempts"] == 1
        previous = history.read("tests/contracts/kernel_v1_snapshot.json")
        assert hashlib.sha256(json.dumps(previous.decode(), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() == evidence["red"]["snapshot"]["protected"]["tests/contracts/kernel_v1_snapshot.json"]
        failure = json.loads(history.read(".quality/veritygate/latest.json"))
        assert failure["passed"] is False and failure["canMerge"] is False
