# Scene-core refactor: ownership and acceptance boundary

This is the first executable slice of the approved cognitive architecture.
The integration branch is `codex/private-chat-dual-route`; changes are developed
on `codex/refactor-scene-core`. The original `refactor/layered-architecture`
branch remains unchanged. Baseline b9b0af4 preserves the existing experiments;
it is not presented as test-first work.

## Stable ownership

- `conversation/channels.py`: trusted audience, canonical scope and inbound
  admission. No platform, database, model or environment imports.
- `skills/audience.py`: closed group/private vocabulary and scoped catalog view.
  No runtime, platform, database or model imports. Undeclared legacy skills
  remain group-only; private use requires an explicit declaration.
- `runtime/skills.py`: installed instruction parsing and progressive selection.
- `skills/host.py`: owner declarations, bounded text and capability dependencies.
  Both catalogs filter directory, always-on text, selection and explicit load.
- `cognition/routes.py`: separate group/private route selection. Existing global
  graph configuration remains a group fallback only. A private graph requires
  its own mode and snapshot. Route failure contributes no graph block.
- `experience/runtime.py`: snapshot loading through an explicit configuration
  namespace; no temporary mutation of process environment.
- `runtime/agent.py`: shared reply path, selects the scene's skill view and
  cognitive route. The existing group prompt is the compatibility baseline.
- `runtime/service.py`: shared lifecycle; private admission does not run the
  group's ambient/probability policy. Delivery and bookkeeping remain shared.
- `harness/tools.py`: fixed tool contracts and execution authorization. Private
  scopes must match the peer; group operations are neither advertised nor
  executed in private conversations.
- `app.py`: composition only, wires the policies and routes.
- `contracts/extension_inputs.py`: optional closed audience declaration; keeps
  old declarations valid and defaults their audience to group.

The only public extension inputs are instruction Skills and declarative task
definitions. Model tools, graph schema, permissions, storage migrations and
scheduler execution are versioned built-ins. No plugin registration is added.
New private/group business workflows must reuse these contracts.

## Test-first acceptance

Tests cover normal selection, invalid audience, missing declarations, always-on
and directory leakage, explicit skill loading, owner overrides, independent
routes and disabled graph loading, canonical scope, private tool authorization,
group compatibility and real private ingest/reply/delivery. Import contracts
enforce the new module boundaries. Full legacy tests remain mandatory.

The fixed VerityGate 0.4.0 tarball is committed under toolchain/vendor. The
language adapter runs pytest/JUnit, Ruff and import-linter. Coverage is measured
on the new scene/audience/cognition modules, with >=90% lines and >=80% branches.
Legacy code is exercised by the full suite but no whole-repository coverage
claim is made. No mutation gate is claimed or configured in this slice.

Red freezes tests, architecture, adapters and thresholds. Green records source
hashes; a fresh final gate is required before merging the slice into the local
integration branch. Local readiness is not a protected GitHub branch rule.

## Deferred slices

Cross-scene memory grants/revocation, the new graph schema/extraction pipeline,
and private declarative scheduling remain separate behavior changes with their
own contracts and red/green evidence. This slice does not silently enable
cross-conversation memory or publish the old experimental graph.
