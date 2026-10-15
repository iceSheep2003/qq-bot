"""Acceptance contract, frozen before scene-core implementation."""

from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import patch

import pytest

from qqbot.infrastructure.tool_handlers import BuiltinHandlers
from qqbot.agent.cognition.routes import SceneRoutes, configured_scene_routes
from qqbot.bootstrap.config import Config
from qqbot.contracts.schema import ContractError
from qqbot.agent.conversation.channels import ChannelPolicy, channel_for
from qqbot.contracts.models import MessageEvent
from qqbot.agent.cognition.experience.decision import LlmDecision
from qqbot.actions.tools.kernel import KernelTools
from qqbot.agent.skills.catalog import SkillCatalog
from qqbot.agent.skills.host import OwnerSkills
import tests.agent.test_behavior_experience_runtime as behavior_helpers
import tests.agent.test_service as lifecycle_helpers


def event(*, group=None, user="7", scope=None):
    return MessageEvent("e1", scope or (f"group:{group}" if group else f"private:{user}"),
                        group, user, "小林", "学习计划", (), False, (), 0)


def skill(root, name, audience=None, *, always=False, priority=0):
    folder = root / name
    folder.mkdir(parents=True)
    fields = f"name: {name}\ndescription: {name}-description\ntriggers: 学习计划\npriority: {priority}\n"
    if audience is not None:
        fields += f"audiences: {audience}\n"
    (folder / "SKILL.md").write_text(f"---\n{fields}---\n{name}-instructions\n")
    if always:
        (folder / "body.md").write_text(f"{name}-always-instructions")
        (folder / "bot-adapter.json").write_text(json.dumps({
            "always_on": True, "triggers": [], "body_file": "body.md",
        }))


def view(catalog, audience):
    factory = getattr(catalog, "for_audience", None)
    assert callable(factory), "Skill catalogs must expose a scoped view"
    return factory(audience)


@pytest.fixture
def catalog(tmp_path):
    skill(tmp_path, "group", "[group]")
    skill(tmp_path, "private", "[private]")
    skill(tmp_path, "shared", "[group, private]")
    skill(tmp_path, "legacy")
    return SkillCatalog(tmp_path)


@pytest.mark.parametrize("audience, expected", [
    ("group", {"group", "shared", "legacy"}),
    ("private", {"private", "shared"}),
])
def test_catalog_selection_and_directory_share_the_same_audience(catalog, audience, expected):
    scoped = view(catalog, audience)
    assert {s.name for s in scoped.skills} == expected
    assert {s.name for s in scoped.select("学习计划")} <= expected
    for candidate in catalog.skills:
        assert (candidate.name + ":") in scoped.catalog_text() if candidate.name in expected else (
            (candidate.name + ":") not in scoped.catalog_text())


@pytest.mark.parametrize("audience", ["", "shared", "all", "PRIVATE", "unknown"])
def test_unknown_audience_is_rejected(catalog, audience):
    with pytest.raises(ValueError):
        view(catalog, audience)


def test_filter_happens_before_the_three_skill_limit(tmp_path):
    for i in range(4):
        skill(tmp_path, f"group{i}", "[group]", priority=100)
    skill(tmp_path, "private", "[private]")
    assert [s.name for s in view(SkillCatalog(tmp_path), "private").select("学习计划")] == ["private"]


def test_always_on_guidance_does_not_cross_scenes(tmp_path):
    skill(tmp_path, "group", "[group]", always=True)
    skill(tmp_path, "private", "[private]", always=True)
    for audience, forbidden in [("group", "private"), ("private", "group")]:
        scoped = view(SkillCatalog(tmp_path), audience)
        assert audience + "-always-instructions" in scoped.stable_instructions()
        assert forbidden + "-always-instructions" not in scoped.stable_instructions()
        assert scoped.select("学习计划") == []


@pytest.mark.parametrize("declaration", ["[private, typo]", "[]", "[group, group]"])
def test_invalid_skill_audiences_are_not_enabled(tmp_path, declaration):
    skill(tmp_path, "bad", declaration)
    assert SkillCatalog(tmp_path).by_name("bad") is None


def owner_catalog(tmp_path):
    root = tmp_path / "skills"
    skill(root, "owner", "[group]")
    folder = root / "owner"
    (folder / "instructions.md").write_text("private-owner-body")
    (folder / "owner-skill.json").write_text(json.dumps({
        "name": "owner", "version": "1", "description": "private-owner-description",
        "activation": "always", "required_capabilities": [],
        "instructions_path": "instructions.md", "context_budget_chars": 1000,
        "priority": 10, "audiences": ["private"],
    }))
    try:
        result = OwnerSkills(root, SkillCatalog(root), frozenset())
    except ContractError as error:
        pytest.fail("Explicit owner audience must be a valid declaration: " + str(error))
    return result


def test_owner_override_is_scoped_at_every_entry(tmp_path):
    catalog = owner_catalog(tmp_path)
    private, group = view(catalog, "private"), view(catalog, "group")
    assert "private-owner-body" in private.stable_instructions()
    assert "owner" not in group.catalog_text()
    assert "private-owner-body" not in group.stable_instructions()
    assert group.by_name("owner") is None
    with pytest.raises(ContractError):
        group.load("owner")
    assert private.load("owner")["instructions"] == "private-owner-body"


def test_explicit_tool_loading_cannot_bypass_skill_audience(catalog):
    handlers = BuiltinHandlers(None, None, catalog)
    with pytest.raises(ContractError):
        handlers.skill_load({"skill_id": "group"}, event())
    loaded = handlers.skill_load({"skill_id": "private"}, event())
    assert loaded["instructions"] == "private-instructions"


@pytest.mark.parametrize("group,scope", [(None, "group:42"), ("42", "private:7"),
                                        (None, "private:8")])
def test_mismatched_event_scope_is_rejected(group, scope):
    policy = ChannelPolicy(frozenset({"42"}), private_enabled=True)
    assert not policy.accepts(event(group=group, scope=scope))


def test_channel_policy_preserves_allowlists_and_private_switch():
    policy = ChannelPolicy(frozenset({"42"}), private_enabled=False)
    assert policy.accepts(event(group="42"))
    assert not policy.accepts(event(group="43"))
    assert not policy.accepts(event())
    assert ChannelPolicy(frozenset(), private_enabled=True).accepts(event())
    assert channel_for(event()) == "private"
    assert channel_for(event(group="42")) == "group"


def test_channel_derivation_rejects_a_forged_scope():
    with pytest.raises(ValueError):
        channel_for(event(scope="private:other"))


def test_private_only_deployment_does_not_require_a_group():
    with patch.dict("os.environ", {"BOT_ONEBOT_TOKEN": "test", "BOT_MODEL_API_KEY": "test",
                                   "BOT_PRIVATE_ENABLED": "true", "BOT_GROUP_ALLOWLIST": ""}, clear=True):
        try:
            config = Config.from_env()
            config.validate()
        except ValueError as error:
            pytest.fail("Private-only deployment must be supported: " + str(error))


class Route:
    def __init__(self, mode):
        self.mode, self.calls = mode, []

    def decide(self, query):
        self.calls.append(query)
        return self.mode


def test_routes_choose_only_the_current_scene():
    group, private = Route("group-plan"), Route("private-plan")
    routes = SceneRoutes(group, private)
    assert routes.decide_for(event(), "private query") == "private-plan"
    assert routes.decide_for(event(group="42"), "group query") == "group-plan"
    assert group.calls == ["group query"]
    assert private.calls == ["private query"]


def test_private_does_not_inherit_global_group_graph(tmp_path):
    graph = behavior_helpers.write_graph(tmp_path / "graph")
    with patch.dict("os.environ", {"BOT_BEHAVIOR_MODE": "graph", "BOT_BEHAVIOR_GRAPH_PATH": str(graph)}, clear=True):
        routes = configured_scene_routes()
    assert routes.group.mode == "graph"
    assert routes.private.mode == "off"
    assert routes.decide_for(event(), "有人问资料在哪里") is None


def test_private_graph_requires_its_own_mode_and_snapshot(tmp_path):
    graph = behavior_helpers.write_graph(tmp_path / "graph")
    with patch.dict("os.environ", {"BOT_BEHAVIOR_MODE": "off", "BOT_PRIVATE_BEHAVIOR_MODE": "hint",
                                   "BOT_PRIVATE_BEHAVIOR_GRAPH_PATH": str(graph)}, clear=True):
        routes = configured_scene_routes()
    assert routes.group.mode == "off"
    assert routes.private.mode == "hint"
    assert routes.decide_for(event(), "有人问复习资料在哪里找").key == "行为经验"


def test_explicit_group_off_overrides_global_graph_without_loading():
    with patch.dict("os.environ", {"BOT_BEHAVIOR_MODE": "graph", "BOT_GROUP_BEHAVIOR_MODE": "off"}, clear=True):
        with patch("qqbot.agent.cognition.experience.runtime.BehaviorGraphRetriever") as loader:
            routes = configured_scene_routes()
    assert routes.group.mode == routes.private.mode == "off"
    loader.assert_not_called()


@pytest.mark.parametrize("mode", ["unknown", "hint", "graph"])
def test_invalid_or_missing_private_snapshot_falls_back(mode):
    with patch.dict("os.environ", {"BOT_PRIVATE_BEHAVIOR_MODE": mode}, clear=True):
        routes = configured_scene_routes()
    assert routes.private.mode == "off"


def test_private_tools_are_scoped_and_group_operations_are_hidden():
    assert "private_enabled" in inspect.signature(KernelTools).parameters
    tools = KernelTools({"conversation_read": lambda a, e: {"messages": []}},
                        allowed_scopes=frozenset({"group:42"}), private_enabled=True)
    allowed = json.loads(asyncio.run(tools.acall("conversation_read", {"limit": 5}, event())))
    assert allowed["status"] == "ok"
    denied = json.loads(asyncio.run(tools.acall("conversation_read", {"limit": 5},
                                               event(scope="private:8"))))
    assert denied["status"] == "denied"
    names = {s["function"]["name"] for s in tools.schemas_for(event())}
    assert {"group_query", "group_action_propose", "interaction_propose"}.isdisjoint(names)
    assert "conversation_read" in names
    assert "group_query" in {s["function"]["name"] for s in tools.schemas_for(event(group="42"))}


def test_private_group_tool_call_is_denied_even_if_the_model_invents_it():
    assert "private_enabled" in inspect.signature(KernelTools).parameters
    calls = []
    tools = KernelTools({"group_query": lambda a, e: calls.append(a)},
                        allowed_scopes=frozenset(), private_enabled=True)
    result = json.loads(asyncio.run(tools.acall("group_query", {"category": "members", "limit": 5}, event())))
    assert result["status"] == "denied"
    assert calls == []


@pytest.fixture
def agent_factory(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("你是 Kinna。")
    fixture = behavior_helpers.AgentIntegrationTests()
    fixture.persona = persona
    return fixture._agent


def test_agent_filters_both_stable_and_dynamic_skill_content(agent_factory, catalog):
    agent = agent_factory()
    agent.skills = catalog
    private_prompt = json.dumps(agent.build_messages(event()), ensure_ascii=False)
    assert "private-instructions" in private_prompt
    assert "group-instructions" not in private_prompt
    assert "group-description" not in private_prompt
    group_prompt = json.dumps(agent.build_messages(event(group="42")), ensure_ascii=False)
    assert "group-instructions" in group_prompt
    assert "private-instructions" not in group_prompt
    assert "private-description" not in group_prompt
    assert "一对一" in agent.build_messages(event())[0]["content"]


def test_agent_uses_scene_route_and_preserves_direct_group_prompt(agent_factory):
    direct = agent_factory(LlmDecision())
    aware = agent_factory(SceneRoutes(LlmDecision(), LlmDecision()))
    assert direct.build_messages(event(group="42")) == aware.build_messages(event(group="42"))
    assert aware.build_messages(event())


def test_agent_injects_only_the_private_routes_decision(agent_factory, tmp_path):
    graph = behavior_helpers.write_graph(tmp_path / "graph")
    with patch.dict("os.environ", {"BOT_BEHAVIOR_MODE": "hint", "BOT_BEHAVIOR_GRAPH_PATH": str(graph)}, clear=True):
        route = behavior_helpers.configured_behavior_decision()
    agent = agent_factory(SceneRoutes(LlmDecision(), route))
    request = replace_event_text(event(), "有人问复习资料在哪里找")
    assert "行为经验" in agent.build_messages(request)[-1]["content"]
    assert "行为经验" not in agent.build_messages(replace_event_text(event(group="42"), request.text))[-1]["content"]


def replace_event_text(value, text):
    from dataclasses import replace
    return replace(value, text=text)


def test_real_private_lifecycle_is_independent_of_group_admission():
    fixture = lifecycle_helpers.ServiceTests()
    fixture.setUp()
    class GroupPolicy:
        async def decide(self, *args, **kwargs):
            raise AssertionError("Group admission must not see a private message")
    service = fixture.service(policy=fixture.policy(private_enabled=True), reply_policy=GroupPolicy())
    async def run():
        assert await service.ingest(event())
        assert await service.decide_reply(event())
        assert await service.execute_turn(event())
        assert fixture.store.message_count("private:7") == 1
        assert [r["role"] for r in fixture.store.actual_recent("private:7")] == ["user", "assistant"]
        assert fixture.store.message_count("group:42") == 0
        assert service.sender.sent[0]["user_id"] == "7"
        await service.aclose()
    try:
        asyncio.run(run())
    finally:
        fixture.tearDown()


def test_service_rejects_cross_scope_before_storing():
    fixture = lifecycle_helpers.ServiceTests()
    fixture.setUp()
    service = fixture.service(policy=fixture.policy(private_enabled=True))
    try:
        assert not asyncio.run(service.ingest(event(scope="private:8")))
        assert fixture.store.message_count("private:8") == 0
    finally:
        asyncio.run(service.aclose())
        fixture.tearDown()
