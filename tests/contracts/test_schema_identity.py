"""Renaming schema identifiers cannot silently relax the fixed contracts."""
import json
from pathlib import Path

from qqbot.contracts import extension_inputs, kernel_v1, lifecycle
from qqbot.contracts.ontology import v1


def test_namespace_migration_preserves_every_non_identity_schema_field():
    before = json.loads(Path(__file__).with_name("schema_identity_baseline.json").read_text())

    def expected(value):
        if isinstance(value, dict):
            return {key: item.replace("urn:qunbot:v1:", "urn:qqbot:v1:") if key == "$id" and isinstance(item, str) else expected(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expected(item) for item in value]
        return value

    actual = {"ontology": v1.specification(), "tools": kernel_v1.specification(), "extensions": extension_inputs.specification(), "lifecycles": lifecycle.specification()}
    assert json.loads(json.dumps(actual)) == expected(before)
