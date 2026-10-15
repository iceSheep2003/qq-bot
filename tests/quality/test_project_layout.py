"""Executable package boundaries and an installable distribution, without QQ."""

import ast
import configparser
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "qunbot"
SECTIONS = {
    "agent", "actions", "features", "infrastructure", "interfaces", "bootstrap", "contracts",
}


def source_root():
    assert SOURCE.is_dir(), "Production Python code belongs in src/qunbot"
    return SOURCE


def test_production_source_has_one_canonical_location():
    assert (source_root() / "__init__.py").is_file()
    assert not (ROOT / "qunbot").exists(), "A second import tree hides stale implementations"
    actual = {p.name for p in SOURCE.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert actual == SECTIONS


def test_no_flat_runtime_or_ambiguous_duplicate_packages_remain():
    source_root()
    for old in ("runtime", "harness", "extensions", "memory", "knowledge", "experience", "storage", "adapters"):
        assert not (SOURCE / old).exists(), old
    assert not (ROOT / "scripts").exists(), "Research, operations and quality tools have separate homes"


def test_test_directories_follow_runtime_responsibilities():
    test_root = ROOT / "tests"
    assert not list(test_root.glob("test_*.py"))
    actual = {p.name for p in test_root.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert actual == SECTIONS | {"quality"}
    for section in actual:
        assert list((test_root / section).glob("test_*.py")), section


def module_imports(path):
    package = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
    package = package.removesuffix(".__init__") if path.name == "__init__.py" else package.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else (node.module or "")


@pytest.mark.parametrize("relative", [
    "contracts", "agent/cognition", "agent/skills", "agent/conversation/channels.py",
    "agent/orchestrator.py", "agent/service.py", "actions/tools/registry.py",
])
def test_stable_decision_boundaries_do_not_import_concrete_infrastructure(relative):
    target = source_root() / relative
    assert target.exists(), relative
    paths = sorted(target.rglob("*.py")) if target.is_dir() else [target]
    assert paths
    forbidden = ("qunbot.infrastructure", "qunbot.features", "qunbot.interfaces", "qunbot.bootstrap")
    if relative == "contracts":
        forbidden += ("qunbot.agent", "qunbot.actions")
    for path in paths:
        imports = list(module_imports(path))
        assert not [name for name in imports if any(name == p or name.startswith(p + ".") for p in forbidden)], path


def test_all_python_sources_parse_and_use_real_new_imports():
    paths = list(source_root().rglob("*.py"))
    assert len(paths) >= 200
    old = ("qunbot.runtime", "qunbot.extensions", "qunbot.harness", "qunbot.storage", "qunbot.adapters", "qunbot.domain", "qunbot.ports", "qunbot.app", "qunbot.config")
    for path in paths:
        for imported in module_imports(path):
            assert not any(imported == p or imported.startswith(p + ".") for p in old), (path, imported)
        assert "sys.modules[" not in path.read_text(), "Do not emulate the removed packages with module aliases"


def test_import_linter_enforces_the_public_contract_boundary():
    config = configparser.ConfigParser()
    config.read(ROOT / ".importlinter")
    contracts = [dict(config[section]) for section in config.sections() if section.startswith("importlinter:contract:")]
    assert len(contracts) >= 6
    assert any(c["source_modules"].strip() == "qunbot.contracts" and "qunbot.infrastructure" in c["forbidden_modules"] for c in contracts)


def test_packaging_and_both_cli_names_use_the_new_composition_root():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]
    assert project["project"]["scripts"]["qunbot"] == "qunbot.bootstrap.app:main"
    assert project["project"]["scripts"]["kinna-bot"] == "qunbot.bootstrap.app:main"
    assert project["build-system"]["build-backend"] == "setuptools.build_meta"


@pytest.mark.parametrize("module", [
    "tools.experiments.bench_behavior_retrieval", "tools.operations.serve_console",
    "tools.operations.voice_smoke", "tools.corpus",
])
def test_documented_developer_tools_start_without_path_injection(module):
    source_root()
    result = subprocess.run([sys.executable, "-m", module, "--help"], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_research_tools_do_not_modify_the_import_search_path():
    source_root()
    paths = sorted((ROOT / "tools").rglob("*.py"))
    assert len(paths) >= 25
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert ast.unparse(node.func) not in {"sys.path.insert", "sys.path.append"}, path


def test_previous_regression_cases_and_assertion_counts_survive_relocation():
    inventory = json.loads((ROOT / ".veritygate/TEST_MIGRATION.json").read_text())
    assert len(inventory) >= 50
    for relative, before in inventory.items():
        tree = ast.parse((ROOT / relative).read_text())
        tests = sorted(n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"))
        assertions = sum(isinstance(n, ast.Assert) or (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert")) for n in ast.walk(tree))
        assert tests == before["tests"], relative
        assert assertions == before["assertions"], relative


def test_console_report_discovery_works_after_source_relocation(tmp_path, monkeypatch):
    source_root()
    from qunbot.interfaces.webui import experiments

    checkout = tmp_path / "checkout"
    fake_source = checkout / "src/qunbot/interfaces/webui/experiments.py"
    fake_source.parent.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    report = checkout / "experiments/behavior-retrieval.json"
    report.parent.mkdir()
    report.write_text("{}")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(experiments, "__file__", str(fake_source))
    assert experiments._resolve_report_path() == report
    explicit = elsewhere / "explicit.json"
    assert experiments._resolve_report_path(explicit) == explicit


def test_built_wheel_imports_every_module_and_contains_runtime_assets(tmp_path):
    source_root()
    wheel_dir = tmp_path / "wheels"
    build = subprocess.run([
        sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
        "--no-index", "--wheel-dir", str(wheel_dir), str(ROOT),
    ], capture_output=True, text=True, timeout=60)
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        files = archive.namelist()
        assert not any(name.startswith(("tests/", "tools/", "data/", ".env")) for name in files)
    target = tmp_path / "installed"
    install = subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
        "--target", str(target), str(wheels[0]),
    ], capture_output=True, text=True, timeout=60)
    assert install.returncode == 0, install.stdout + install.stderr
    # cwd is outside the checkout and PYTHONPATH contains only the wheel target.
    # Dependencies come from the test interpreter; source imports must come from the wheel.
    env = dict(os.environ, PYTHONPATH=str(target), PYTHONDONTWRITEBYTECODE="1")
    probe = '''import importlib, json, pkgutil
from importlib.resources import files
from pathlib import Path
import qunbot
assert Path(qunbot.__file__).is_relative_to(Path.cwd() / "installed")
names = [m.name for m in pkgutil.walk_packages(qunbot.__path__, "qunbot.")]
for name in names:
    importlib.import_module(name)
assets = {
    "qunbot.interfaces.webui": ["static/index.html", "static/app.js", "static/app.css", "static/kinna.png", "static/kinna-avatar.png", "docs/GUIDE.md"],
    "qunbot.agent.identity.guard": ["rules.json"],
    "qunbot.features.slang": ["data/common_words.txt", "data/slang_review.example.json"],
    "qunbot.features.scheduled_chat": ["data/proactive_groups.example.json"],
}
for package, paths in assets.items():
    for relative in paths:
        assert files(package).joinpath(relative).read_bytes(), (package, relative)
print(json.dumps({"modules": len(names)}))
'''
    result = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["modules"] >= 200
    for command in ("qunbot", "kinna-bot"):
        result = subprocess.run([sys.executable, str(target / "bin" / command), "--help"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "Kinna bot" in result.stdout
        assert "--check" in result.stdout
