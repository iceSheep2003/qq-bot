# Reviewed protocol namespace migration

Baseline source: feb0c87, recovered from the isolated pre-layout checkout.
The old snapshot was preserved in Git and is included in the archived old
quality inputs. Comparison of complete JSON schema documents shows exactly
41 $id namespace changes from urn:qunbot:v1: to urn:qqbot:v1:. Every other
JSON field, constraint, required field, enum and reference target is identical.
Python tuples are serialized to JSON arrays on both sides before comparison.

The expected fingerprint fixture is deliberately updated before the new
Code Quality Gate red phase. A new regression test compares complete current
specifications to the immutable baseline, allowing only those $id changes.
The v1 data shape is unchanged; schema identifiers use the new namespace.
No failed report or existing quality evidence is edited. The prior naming
green failure remains recorded as a real failure.
