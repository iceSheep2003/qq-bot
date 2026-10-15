"""Public branding remains consistent without breaking installed entry points."""
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cli_help_identifies_kinna_bot_without_requiring_configuration():
    result = subprocess.run(
        [sys.executable, "-c", "from qunbot.app import main; main()", "--help"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert "Kinna bot" in result.stdout
    assert "--check" in result.stdout


def test_console_uses_kinna_bot_in_visible_branding():
    html = (ROOT / "qunbot/extensions/webui/static/index.html").read_text()
    assert "<title>Kinna bot 控制台</title>" in html
    assert "<b>Kinna bot</b>" in html
    assert "QunBot" not in html
    assert "QUNBOT" not in html


def test_project_description_and_readme_identify_kinna_bot():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert "Kinna bot" in project["description"]
    assert project["scripts"]["qunbot"] == "qunbot.app:main"
    assert (ROOT / "README.md").read_text().startswith("# Kinna bot\n")
