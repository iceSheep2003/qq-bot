from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from qqbot.interfaces.webui.envfile import EnvConfigStore
from qqbot.interfaces.webui.route_query import RouteConsoleQuery


def project(tmp_path: Path, **values) -> RouteConsoleQuery:
    lines = [f"{key}={value}" for key, value in values.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return RouteConsoleQuery(EnvConfigStore(tmp_path / ".env"), tmp_path)


def snapshot(tmp_path: Path, name="g1", manifest=None) -> str:
    target = tmp_path / "data/private/derived" / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "graph.json").write_text("{}", encoding="utf-8")
    if manifest is not None:
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return f"data/private/derived/{name}/graph.json"


def test_graph_mode_without_a_snapshot_falls_back_to_off_and_says_why(tmp_path):
    overview = project(tmp_path, BOT_BEHAVIOR_MODE="graph").overview()

    assert overview["graph"]["mode"] == "graph"
    assert overview["graph"]["effective"] == "off"
    assert "回落" in overview["graph"]["reason"]


def test_graph_mode_with_an_existing_snapshot_is_effective(tmp_path):
    path = snapshot(tmp_path)
    overview = project(tmp_path, BOT_BEHAVIOR_MODE="hint", BOT_BEHAVIOR_GRAPH_PATH=path).overview()

    assert overview["graph"]["snapshot_exists"] is True
    assert overview["graph"]["effective"] == "hint"
    assert "提示" in overview["graph"]["reason"]


def test_unknown_mode_value_is_normalised_to_off(tmp_path):
    overview = project(tmp_path, BOT_BEHAVIOR_MODE="turbo").overview()

    assert overview["graph"]["mode"] == "off"


def test_wiki_switch_reports_the_directory_it_would_read(tmp_path):
    overview = project(tmp_path, BOT_WIKI_ENABLED="true", BOT_WIKI_ROOT="data/wiki").overview()

    assert overview["wiki"] == {"enabled": True, "root": "data/wiki", "root_exists": False}


def test_artifacts_summarise_real_manifests_and_count_missing_cases(tmp_path):
    snapshot(tmp_path, "old", {"status": "merged", "proposals": 10, "missing_cases": ["a", "b"]})
    snapshot(tmp_path, "new", {"status": "final", "proposals": 20})

    result = project(tmp_path).artifacts()

    assert result["exists"] is True
    assert result["total"] == 2
    by_name = {item["name"]: item for item in result["items"]}
    assert by_name["old"]["manifest"]["missing_cases"] == 2
    assert by_name["new"]["manifest"]["proposals"] == 20
    assert all(item["path"].startswith("data/private/derived/") for item in result["items"])


def test_artifacts_are_explicit_when_the_directory_is_missing(tmp_path):
    assert project(tmp_path).artifacts() == {
        "root": "data/private/derived",
        "exists": False,
        "items": [],
    }


def test_apply_rejects_switches_outside_the_routing_board(tmp_path):
    with pytest.raises(ValueError, match="不支持的链路开关"):
        project(tmp_path).apply({"BOT_MODEL_NAME": "other"})


def test_apply_rejects_an_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match="off / hint / graph"):
        project(tmp_path).apply({"BOT_BEHAVIOR_MODE": "graphish"})


def test_apply_rejects_a_snapshot_path_that_does_not_exist(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        project(tmp_path).apply({"BOT_BEHAVIOR_GRAPH_PATH": "data/private/derived/missing/graph.json"})


def test_apply_writes_the_env_file_and_requires_a_restart(tmp_path):
    path = snapshot(tmp_path)
    query = project(tmp_path)
    result = query.apply({"BOT_BEHAVIOR_MODE": "graph", "BOT_BEHAVIOR_GRAPH_PATH": path})

    assert result["updated"] == ["BOT_BEHAVIOR_MODE", "BOT_BEHAVIOR_GRAPH_PATH"]
    assert result["restart_required"] is True
    assert "BOT_BEHAVIOR_MODE=graph" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert query.overview()["graph"]["effective"] == "graph"


def test_apply_ignores_an_empty_change_set(tmp_path):
    with pytest.raises(ValueError):
        project(tmp_path).apply({})


def test_llm_links_expose_configured_state_per_link(tmp_path):
    links = project(
        tmp_path,
        BOT_MODEL_NAME="grok",
        BOT_MODEL_BASE_URL="https://api.example/v1",
        BOT_TTS_MODEL="cosyvoice-v3-flash",
    ).overview()["llm"]

    by_id = {link["id"]: link for link in links}
    assert by_id["chat"]["configured"] is True
    assert by_id["vision"]["configured"] is False
    assert by_id["tts"]["model"] == "cosyvoice-v3-flash"


def test_probe_reports_failure_without_a_snapshot(tmp_path):
    result = project(tmp_path).probe()

    assert result["ok"] is False
    assert "快照不可用" in result["detail"]


def test_probe_reports_the_snapshot_the_runtime_loader_accepted(tmp_path, monkeypatch):
    from qqbot.agent.cognition.experience import runtime as experience_runtime

    monkeypatch.setattr(
        experience_runtime,
        "configured_behavior_experience",
        lambda settings: type("Experience", (), {"retriever": type("R", (), {"snapshot": "g1"})()})(),
    )
    result = project(tmp_path, BOT_BEHAVIOR_MODE="hint").probe()

    assert result["ok"] is True
    assert result["snapshot"] == "g1"
    assert "hint" in result["detail"]


def test_artifacts_skip_directories_without_a_snapshot_marker(tmp_path):
    (tmp_path / "data/private/derived/empty").mkdir(parents=True)
    (tmp_path / "data/private/derived/loose.txt").write_text("x", encoding="utf-8")
    snapshot(tmp_path, "real")

    result = project(tmp_path).artifacts()

    assert result["total"] == 1
    assert result["items"][0]["name"] == "real"


def test_a_broken_manifest_keeps_the_artifact_but_drops_its_summary(tmp_path):
    target = tmp_path / "data/private/derived/broken"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{not json", encoding="utf-8")

    item = project(tmp_path).artifacts()["items"][0]

    assert item["marker"] == "manifest.json"
    assert "manifest" not in item


@pytest.mark.parametrize("limit", [0, 51])
def test_artifacts_reject_an_out_of_range_limit(tmp_path, limit):
    with pytest.raises(ValueError, match="limit must be between 1 and 50"):
        project(tmp_path).artifacts(limit=limit)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def model_project(tmp_path, opener, **values):
    base = {"BOT_MODEL_NAME": "deepseek-flash", "BOT_MODEL_BASE_URL": "https://api.deepseek.com",
            "BOT_MODEL_API_KEY": "sk-test"}
    base.update(values)
    lines = [f"{key}={value}" for key, value in base.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return RouteConsoleQuery(EnvConfigStore(tmp_path / ".env"), tmp_path, opener=opener, clock=lambda: 0.0)


def test_model_probe_reports_the_model_that_actually_answered(tmp_path):
    query = model_project(tmp_path, lambda request, timeout: Response(b'{"model": "deepseek-flash-1017"}'))

    result = query.probe_model()

    assert result["ok"] is True
    assert result["model"] == "deepseek-flash-1017"
    assert "连通" in result["detail"]


def test_model_probe_turns_a_bad_key_into_a_readable_reason(tmp_path):
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {},
            io.BytesIO(b'{"error": {"message": "Authentication Fails"}}'),
        )

    result = model_project(tmp_path, opener).probe_model()

    assert result["ok"] is False
    assert result["status"] == 401
    assert "密钥无效" in result["detail"]
    assert "Authentication Fails" in result["detail"]


def test_model_probe_reports_an_unreachable_endpoint_instead_of_guessing(tmp_path):
    def opener(request, timeout):
        raise urllib.error.URLError("Name or service not known")

    result = model_project(tmp_path, opener).probe_model()

    assert result["ok"] is False
    assert "连不上" in result["detail"]


def test_model_probe_refuses_to_run_without_a_key(tmp_path):
    result = model_project(tmp_path, lambda request, timeout: Response(b"{}"), BOT_MODEL_API_KEY="").probe_model()

    assert result["ok"] is False
    assert "API Key" in result["detail"]


def test_model_probe_flags_a_non_json_answer_as_a_wrong_path(tmp_path):
    result = model_project(tmp_path, lambda request, timeout: Response(b"<html>404</html>")).probe_model()

    assert result["ok"] is False
    assert "/v1" in result["detail"]
