"""Professional package identity with persisted deployment compatibility."""

import ast
import importlib
import importlib.util
import subprocess
import sys
import tomllib

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src/qqbot"


def require_package():
    assert (PACKAGE / "__init__.py").is_file(), "The canonical import package is qqbot"


def test_canonical_package_is_qqbot_without_an_old_namespace_proxy():
    require_package()
    assert not (ROOT / "src/qunbot").exists()
    package = importlib.import_module("qqbot")
    assert Path(package.__file__).resolve() == (PACKAGE / "__init__.py").resolve()
    assert importlib.util.find_spec("qunbot") is None


def test_distribution_and_cli_names_are_explicit():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "kinna-bot"
    scripts = project["scripts"]
    assert set(scripts) == {"qqbot", "kinna-bot"}
    for command in ("qqbot", "kinna-bot"):
        assert scripts.get(command) == "qqbot.bootstrap.app:main"


def test_application_and_developer_tools_do_not_import_qunbot():
    require_package()
    files = [*PACKAGE.rglob("*.py"), *(ROOT / "tools").rglob("*.py")]
    assert len(files) >= 230
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            imports = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
            assert not any(name == "qunbot" or name.startswith("qunbot.") for name in imports), path


def test_all_installed_commands_remain_usable_without_model_configuration():
    require_package()
    assert not (Path(sys.executable).parent / "qunbot").exists()
    for command in ("qqbot", "kinna-bot"):
        executable = Path(sys.executable).parent / command
        assert executable.is_file(), command
        result = subprocess.run([sys.executable, str(executable), "--help"], cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "Kinna bot" in result.stdout
        assert "--check" in result.stdout


def config_for_test(monkeypatch):
    require_package()
    from qqbot.bootstrap.config import Config

    monkeypatch.setenv("BOT_GROUP_ALLOWLIST", "42")
    monkeypatch.setenv("BOT_ONEBOT_TOKEN", "fixture")
    monkeypatch.setenv("BOT_MODEL_API_KEY", "fixture")
    monkeypatch.delenv("BOT_DB_PATH", raising=False)
    return Config.from_env()


def test_fresh_deployment_uses_the_canonical_database_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = config_for_test(monkeypatch)
    assert config.db_path == Path("data/qqbot.sqlite3")
    validate = getattr(config, "validate_storage_selection", None)
    assert callable(validate)
    validate()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("entry", ["application", "check"])
def test_upgrade_refuses_a_silent_empty_database(entry, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    previous = tmp_path / "data/previous.sqlite3"
    previous.write_bytes(b"existing data fixture")
    config = config_for_test(monkeypatch)
    assert config.db_path == Path("data/qqbot.sqlite3")
    from qqbot.bootstrap.app import BotApp, main
    from qqbot.contracts.models import ConfigError

    if entry == "application":
        with pytest.raises(ConfigError, match="BOT_DB_PATH"):
            BotApp(config)
    else:
        monkeypatch.setattr(sys, "argv", ["qqbot", "--check"])
        with pytest.raises(SystemExit, match="BOT_DB_PATH"):
            main()
    assert previous.read_bytes() == b"existing data fixture"
    assert not (tmp_path / "data/qqbot.sqlite3").exists()


def test_explicit_existing_database_is_respected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_for_test(monkeypatch)
    from qqbot.bootstrap.config import Config

    previous = tmp_path / "archive.sqlite3"
    previous.write_bytes(b"existing data fixture")
    monkeypatch.setenv("BOT_DB_PATH", str(previous))
    config = Config.from_env()
    assert config.db_path == previous
    validate = getattr(config, "validate_storage_selection", None)
    assert callable(validate)
    validate()
    assert previous.read_bytes() == b"existing data fixture"


def test_existing_canonical_database_is_not_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/qqbot.sqlite3").touch()
    (tmp_path / "data/previous.sqlite3").touch()
    config = config_for_test(monkeypatch)
    validate = getattr(config, "validate_storage_selection", None)
    assert callable(validate)
    validate()


def test_live_source_contains_no_legacy_package_identifiers():
    require_package()
    for path in PACKAGE.rglob("*"):
        if path.suffix in {".py", ".js", ".html", ".json", ".md"}:
            assert "qunbot" not in path.read_text().lower(), path
