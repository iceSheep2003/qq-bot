"""Synthetic protocol fixtures, never collected chat data or live QQ calls."""

from __future__ import annotations

import ast
import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from qunbot.contracts import extension_inputs, kernel_v1, lifecycle
from qunbot.contracts.schema import ContractError, validate
from qunbot.ontology import v1

SCOPE = "agent:test/account:qq/group:42"


def ref(node):
    return {k: node[k] for k in ("id", "type", "scope")}


def node(entity_type, identity, **fields):
    return {"id": identity, "type": entity_type, "scope": SCOPE, "schema_version": 1,
            "created_at": 1, "updated_at": 1, "source_refs": [], "visibility": "group", **fields}


def fixture():
    person = node("Person", "person:a", platform="qq", account_ref="account:a")
    message = node("Message", "message:1", platform_message_id="42", author_ref=ref(person),
                   event_time=1, recorded_at=1, direction="inbound", origin="member",
                   parts=[{"kind": "text", "text": "测试协议文本"}], reply_to_ref=None, delivery_status="received")
    predicate = node("Concept", "concept:preference", name="preference", aliases=[], kind="predicate",
                     definition="A self-reported preference", status="accepted", subject_types=["Person"],
                     object_types=["Concept"], value_kinds=["text"])
    claim = node("Claim", "claim:1", subject_ref=ref(person), predicate_ref=ref(predicate), object_ref=None,
                 typed_value={"kind": "text", "value": "protocol fixture"}, asserted_by_ref=ref(person),
                 polarity="positive", modality="assertion", epistemic_status="accepted_as_report",
                 source_kind="self_report", observed_at=1, recorded_at=1, valid_from=None, valid_to=None)
    evidence = node("Evidence", "evidence:1", source_ref=ref(message), start=0, end=6,
                    source_sha256=hashlib.sha256("测试协议文本".encode()).hexdigest(), extractor_version="test-v1", role="supports")
    edge = {"id": "edge:1", "scope": SCOPE, "schema_version": 1, "relation": "has_evidence",
            "source_ref": ref(claim), "target_ref": ref(evidence), "source_refs": [], "created_at": 1}
    return [person, message, predicate, claim, evidence], [edge]


def sample(schema):
    """Build minimum synthetic values for schema-shape tests, not gold labels."""
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("oneOf", "anyOf"):
        if key in schema:
            return sample(schema[key][0])
    kind = schema.get("type", "object")
    if kind == "null":
        return None
    if kind == "object":
        return {key: sample(value) for key, value in schema.get("properties", {}).items() if key in schema.get("required", [])}
    if kind == "array":
        return [sample(schema["items"]) for _ in range(schema.get("minItems", 0))]
    if kind in {"integer", "number"}:
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if schema.get("pattern") == "^[a-f0-9]{64}$":
        return "0" * 64
    if schema.get("pattern", "").startswith("^asset:"):
        return "asset:test"
    return "x" * max(1, schema.get("minLength", 1))


class OntologyContractTests(unittest.TestCase):
    def test_all_schemas_are_valid_2020_12(self):
        spec = v1.specification()
        self.assertEqual(len(spec["entities"]), 19)
        self.assertEqual(len(spec["relations"]), 21)
        for kind in v1.entity_types():
            with self.subTest(kind=kind):
                schema = v1.entity_schema(kind)
                Draft202012Validator.check_schema(schema)
                validate(schema, sample(schema))
        Draft202012Validator.check_schema(spec["edge"])

    def test_valid_report_with_traceable_support(self):
        nodes, edges = fixture()
        v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_unknown_types_and_versions_rejected(self):
        for mutation in ({"type": "GameMatch"}, {"type": []}, {"schema_version": 2}, {"secret_admin": True}):
            nodes, edges = fixture()
            nodes[0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_same_name_is_not_identity_and_duplicate_account_rejected(self):
        nodes, edges = fixture()
        nodes.append(node("Person", "person:b", platform="qq", account_ref="account:b"))
        v1.validate_graph(nodes, edges, scope=SCOPE)
        nodes[-1]["account_ref"] = "account:a"
        with self.assertRaisesRegex(ContractError, "Duplicate person"):
            v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_cross_scope_missing_and_forged_type_refs_rejected(self):
        for mutation in ({"scope": "group:other"}, {"id": "missing"}, {"type": "Group"}):
            nodes, edges = fixture()
            edges[0]["target_ref"].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_unknown_and_reversed_relations_rejected(self):
        nodes, edges = fixture()
        edges[0]["relation"] = "loves_forever"
        with self.assertRaises(ContractError):
            v1.validate_graph(nodes, edges, scope=SCOPE)
        edges[0]["relation"] = "has_evidence"
        edges[0]["source_ref"], edges[0]["target_ref"] = edges[0]["target_ref"], edges[0]["source_ref"]
        with self.assertRaises(ContractError):
            v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_context_or_refutation_cannot_prove_accepted_report(self):
        for role in ("context", "refutes"):
            nodes, edges = fixture()
            nodes[-1]["role"] = role
            with self.subTest(role=role), self.assertRaisesRegex(ContractError, "supporting evidence"):
                v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_no_evidence_is_allowed_only_for_candidate(self):
        nodes, _ = fixture()
        with self.assertRaises(ContractError):
            v1.validate_graph(nodes, [], scope=SCOPE)
        nodes[3]["epistemic_status"] = "candidate"
        v1.validate_graph(nodes, [], scope=SCOPE)

    def test_evidence_span_and_fingerprint_verified(self):
        for mutation in ({"end": 1000}, {"start": 6, "end": 6}, {"source_sha256": "0" * 64}):
            nodes, edges = fixture()
            nodes[-1].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_predicate_and_literal_constraints(self):
        for mutation in ({"kind": "topic"}, {"subject_types": ["Group"]}, {"value_kinds": ["number"]}, {"status": "candidate"}):
            nodes, edges = fixture()
            nodes[2].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_claim_has_exactly_one_value(self):
        nodes, edges = fixture()
        nodes[3]["object_ref"] = ref(nodes[2])
        with self.assertRaises(ContractError):
            v1.validate_graph(nodes, edges, scope=SCOPE)
        nodes[3]["object_ref"] = None
        nodes[3]["typed_value"] = None
        with self.assertRaises(ContractError):
            v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_relation_must_agree_with_authoritative_field(self):
        nodes, edges = fixture()
        nodes.append(node("Person", "person:b", platform="qq", account_ref="account:b"))
        edges[0].update(relation="authored", source_ref=ref(nodes[-1]), target_ref=ref(nodes[1]))
        with self.assertRaises(ContractError):
            v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_vote_requires_real_author_and_unique_voter(self):
        nodes, edges = fixture()
        proposal = node("Proposal", "proposal:1", kind="title", actor_ref=ref(nodes[0]), target_ref=ref(nodes[0]),
                        content="fixture", status="awaiting_consent", expires_at=10, approval_refs=[])
        vote = node("Vote", "vote:1", proposal_ref=ref(proposal), voter_ref=ref(nodes[0]), decision="approve", message_ref=ref(nodes[1]))
        nodes.extend([proposal, vote])
        v1.validate_graph(nodes, edges, scope=SCOPE)
        duplicate = {**vote, "id": "vote:2"}
        with self.assertRaisesRegex(ContractError, "Duplicate voter"):
            v1.validate_graph([*nodes, duplicate], edges, scope=SCOPE)
        other = node("Person", "person:b", platform="qq", account_ref="account:b")
        vote["voter_ref"] = ref(other)
        with self.assertRaisesRegex(ContractError, "authored by the voter"):
            v1.validate_graph([*nodes, other], edges, scope=SCOPE)

    def test_media_kind_and_readable_text_are_separate(self):
        nodes, edges = fixture()
        asset = node("MediaAsset", "asset:1", storage_ref="asset:1", kind="audio", mime="audio/wav", bytes=100, duration_ms=1000, sha256="0"*64)
        nodes.append(asset)
        nodes[1]["parts"].append({"kind": "image", "asset_ref": ref(asset)})
        with self.assertRaisesRegex(ContractError, "asset kind"):
            v1.validate_graph(nodes, edges, scope=SCOPE)
        nodes[1]["parts"][-1]["kind"] = "audio"
        v1.validate_graph(nodes, edges, scope=SCOPE)

    def test_repeated_link_is_rejected(self):
        nodes, edges = fixture()
        duplicate = deepcopy(edges[0])
        duplicate["id"] = "edge:2"
        with self.assertRaisesRegex(ContractError, "Duplicate relation"):
            v1.validate_graph(nodes, [*edges, duplicate], scope=SCOPE)

    def test_public_descriptors_cannot_mutate_core(self):
        before = v1.specification_hash()
        spec = v1.specification()
        spec["entities"]["Person"]["properties"]["permission"] = {"const": "admin"}
        spec["relations"]["new_relation"] = {}
        self.assertEqual(before, v1.specification_hash())
        self.assertFalse(hasattr(v1, "register"))


class KernelToolContractTests(unittest.TestCase):
    def test_exact_tool_list(self):
        self.assertEqual(kernel_v1.tool_names(), tuple(sorted((
            "conversation_read", "knowledge_search", "knowledge_evidence", "group_query", "media_catalog",
            "vision_analyze", "audio_transcribe", "interaction_propose", "group_action_propose", "skill_load",
        ))))

    def test_all_inputs_and_outputs_validate(self):
        for name in kernel_v1.tool_names():
            with self.subTest(tool=name):
                input_schema, output_schema = kernel_v1.tool_input_schema(name), kernel_v1.tool_output_schema(name)
                Draft202012Validator.check_schema(input_schema)
                Draft202012Validator.check_schema(output_schema)
                kernel_v1.validate_tool_input(name, sample(input_schema))
                result = sample(output_schema)
                result["error"] = None
                kernel_v1.validate_tool_output(name, result)

    def test_host_authority_fields_are_not_model_arguments(self):
        for name in kernel_v1.tool_names():
            for field in ("scope", "actor_id", "permissions", "authorization", "api_key"):
                arguments = sample(kernel_v1.tool_input_schema(name))
                arguments[field] = "injected"
                with self.subTest(tool=name, field=field), self.assertRaises(ContractError):
                    kernel_v1.validate_tool_input(name, arguments)

    def test_arbitrary_urls_cannot_enter_perception_tools(self):
        for name in ("vision_analyze", "audio_transcribe"):
            arguments = sample(kernel_v1.tool_input_schema(name))
            arguments["asset_id"] = "https://example.invalid/private"
            with self.subTest(tool=name), self.assertRaises(ContractError):
                kernel_v1.validate_tool_input(name, arguments)

    def test_group_actions_require_their_specific_parameters(self):
        for bad in ({"kind": "mute", "target_id": "person:a"}, {"kind": "title", "target_id": "person:a", "title": ""},
                    {"kind": "install", "code": "anything"}, {"kind": "mute", "target_id": "person:a", "duration_seconds": -1, "reason": ""}):
            with self.subTest(case=bad), self.assertRaises(ContractError):
                kernel_v1.validate_tool_input("group_action_propose", bad)

    def test_error_result_cannot_include_fake_success_data(self):
        result = {"status": "unknown", "trace_id": "trace:1", "data": None,
                  "error": {"code": "unknown", "message": "Outcome requires verification"}}
        kernel_v1.validate_tool_output("interaction_propose", result)
        result["data"] = {"request_id": "r:1", "kind": "poke", "state": "succeeded"}
        with self.assertRaises(ContractError):
            kernel_v1.validate_tool_output("interaction_propose", result)

    def test_budget_finite_json_and_safe_errors(self):
        for arguments in ({"query": "private-text" * 2000, "limit": 4}, {"query": "x", "limit": True},
                          {"query": "x", "limit": float("nan")}):
            with self.subTest(case=type(arguments["limit"])), self.assertRaises(ContractError) as caught:
                kernel_v1.validate_tool_input("knowledge_search", arguments)
            self.assertNotIn("private-text", str(caught.exception))

    def test_skill_does_not_register_schemas(self):
        before = (v1.specification_hash(), kernel_v1.specification_hash())
        descriptor = kernel_v1.model_tool_schemas()
        descriptor[0]["function"]["name"] = "new_game"
        self.assertEqual(len(kernel_v1.tool_names()), 10)
        self.assertEqual(before, (v1.specification_hash(), kernel_v1.specification_hash()))


class LifecycleContractTests(unittest.TestCase):
    def test_all_declared_transitions(self):
        for kind, transitions in lifecycle.specification()["lifecycles"].items():
            for transition in transitions:
                self.assertEqual(lifecycle.next_state(kind, transition["from"], transition["event"]), transition["to"])

    def test_uncertain_execution_requires_confirmation_not_retry(self):
        self.assertEqual(lifecycle.next_state("execution", "executing", "uncertain_result"), "unknown")
        with self.assertRaises(ContractError):
            lifecycle.next_state("execution", "unknown", "authorize")
        self.assertEqual(lifecycle.next_state("execution", "unknown", "confirmed_success"), "succeeded")

    def test_terminal_states_cannot_be_reopened(self):
        for kind, states in (("execution", ("succeeded", "failed", "denied", "cancelled")),
                            ("proposal", ("succeeded", "failed", "expired", "rejected", "cancelled"))):
            for state in states:
                with self.subTest(kind=kind, state=state), self.assertRaises(ContractError):
                    lifecycle.next_state(kind, state, "authorize")

    def test_proposal_cannot_skip_consent(self):
        with self.assertRaises(ContractError):
            lifecycle.next_state("proposal", "awaiting_consent", "authorize")

    def test_import_boundaries(self):
        root = Path(__file__).resolve().parents[1] / "qunbot"
        for package in ("contracts", "ontology"):
            for path in (root / package).glob("*.py"):
                for statement in ast.walk(ast.parse(path.read_text())):
                    if isinstance(statement, ast.ImportFrom):
                        module = statement.module or ""
                        self.assertFalse(any(word in module.split(".") for word in ("runtime", "extensions", "adapters", "storage")), path)


class ExtensionDeclarationTests(unittest.TestCase):
    def skill(self):
        return {"name": "chat", "version": "1", "description": "Synthetic fixture", "activation": "on_demand",
                "required_capabilities": [], "instructions_path": "skills/chat/SKILL.md", "context_budget_chars": 1000, "priority": 0}

    def test_schema_documents_are_valid(self):
        for key in ("skill",):
            Draft202012Validator.check_schema(extension_inputs.specification()[key])

    def test_skill_cannot_declare_python_code_or_ontology_types(self):
        for field in ("entry", "command", "schema_contributions", "migration_steps", "permissions"):
            value = self.skill()
            value[field] = "anything"
            with self.subTest(field=field), self.assertRaises(ContractError):
                extension_inputs.validate_skill(value)

    def test_skill_path_cannot_escape_root(self):
        for path in ("../SKILL.md", "skills/../../SKILL.md", "/private/SKILL.md", "https://example.invalid/SKILL.md", "skill.py"):
            value = self.skill()
            value["instructions_path"] = path
            with self.subTest(path=path), self.assertRaises(ContractError):
                extension_inputs.validate_skill(value)

    def test_only_skill_metadata_without_structure_changes(self):
        before = (v1.specification_hash(), kernel_v1.specification_hash())
        extension_inputs.validate_skill(self.skill())
        self.assertEqual(before, (v1.specification_hash(), kernel_v1.specification_hash()))


class SpecificationSnapshotTests(unittest.TestCase):
    def test_v1_draft_contract_fingerprints(self):
        expected = json.loads((Path(__file__).parent / "kernel_v1_snapshot.json").read_text())
        actual = {"ontology": v1.specification_hash(), "tools": kernel_v1.specification_hash(),
                  "lifecycles": lifecycle.specification_hash(), "extensions": extension_inputs.specification_hash()}
        self.assertEqual(expected["fingerprints"], actual, "Contract changed: review and explicitly update the version/snapshot")


if __name__ == "__main__":
    unittest.main()
