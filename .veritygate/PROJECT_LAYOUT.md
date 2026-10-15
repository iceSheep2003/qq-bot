# Kinna bot canonical package layout

This task reorganizes the existing implementation for teaching an Agent and
maintaining it. It does not introduce new chat behavior, graph algorithms,
cross-scene memory sharing, permissions or public plugin APIs.

## Ownership agreed before implementation

- src/qunbot/agent: online orchestration plus perception, conversation, identity,
  memory, cognition and instruction Skills. Skill catalog leaves runtime.
- src/qunbot/actions: fixed tool registry/kernel, reply planning/delivery,
  native interactions and the scheduler.
- src/qunbot/contracts: shared models, ports, protocol schemas and ontology.
  BehaviorDecision is a shared value, so its definition moves here.
- src/qunbot/infrastructure: QQ/model adapters, database repositories and concrete
  built-in tool handlers. Behavior extraction using Instructor belongs here,
  not under the pure cognitive decision package.
- src/qunbot/features: version-maintained opt-in integrations, with a fixed local
  allowlist. They are not a third public extension mechanism.
- src/qunbot/interfaces: owner WebUI and management queries.
- src/qunbot/bootstrap: configuration and composition root.

Import-linter freezes six boundaries: contracts, scene admission, Skill audience,
Skill catalog, cognitive decision and the Agent loop. Feature registration and
legacy memory internals are not claimed to be fully pure or cycle-free.

Production code has one canonical source under src. Imports are rewritten to
real modules; there are no sys.modules aliases, compatibility duplicate trees or
search-path injection. The distribution/import name qunbot and existing command
qunbot remain; kinna-bot becomes a second command for the same composition root.
Runtime state, .env settings, personas and real schedule files stay in place.
Package data must include console assets, identity rules, slang dictionary and
declarative schedule examples in an actual built wheel.

## Project management

Tests mirror the seven responsibility groups plus quality. Research tooling is
in tools/experiments, operational utilities in tools/operations, corpus preparation
in tools/corpus and quality adapters in tools/quality. They run with python -m.
Documents separate current architecture/guides, future design, research and history.
VS Code opens one source tree and excludes only generated caches/private state.

## Frozen acceptance and migrated tests

Prior integration baseline: feb0c87. Existing tests are relocated before red; only
import targets, source/fixture paths, log namespaces and renamed boundary words
change. TEST_MIGRATION.json records each original test function name and assertion
count from this baseline. Existing protocol fingerprint fixtures remain unchanged.
LAYOUT_MAP.json records the explicit old/new file and module names. The previous
three test tasks/evidence are preserved exactly; they still describe their own
Git snapshots. Adapters and the active check list are migrated before freezing.
No existing threshold or full-regression check is removed or relaxed.

New acceptance checks canonical layout, forbidden dependencies, no old imports,
retained test names/assertion counts, operational/research entry points, report
lookup outside the checkout cwd, and a real offline wheel build/install. A fresh
subprocess imports all wheel modules and reads packaged resources from outside
the repository, and exercises both installed CLI names. These checks do not
contact a live model or QQ account.

The task starts with assertion failures against the old production tree. After
implementation, full regression, lint, six import contracts, and the same scene
module coverage thresholds run, followed by a fresh final gate.
