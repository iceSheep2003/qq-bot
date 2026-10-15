# Scene-core refactor

This slice separates the trusted conversation scene, instruction audience and
optional cognitive route while reusing the established reply/delivery path.
It does not rebuild the experimental graph or enable cross-scene personal
memory. Those remain separate test-first changes.

## Configuration and compatibility

Group graph mode uses `BOT_GROUP_BEHAVIOR_MODE` when explicitly set, otherwise
the existing `BOT_BEHAVIOR_MODE`. Existing global snapshot and scoring settings
remain the group configuration. `off` does not load its snapshot index.

Private graph mode defaults to `off`. It uses only
`BOT_PRIVATE_BEHAVIOR_MODE`, `BOT_PRIVATE_BEHAVIOR_GRAPH_PATH` and the matching
private calibration/budget/weight settings. A missing or malformed private
snapshot falls back to the direct route; it never borrows the group snapshot.
The old graph is experimental equipment, not the new cognitive graph design.

`BOT_PRIVATE_ENABLED=true` permits private ingress and scoped built-in tools.
A private-only deployment may leave the group allowlist empty. Scope must match
the actual peer; group-only tools are hidden and denied in private. The private
reply policy does not run group ambient probability or group interjection quotas.

The group stable prefix remains compatible when configuration is unchanged.
Private prefixes use their own allowed skill view and one-to-one guidance.
Prefix usage hashes are calculated from the actual request prefix.

## Skill audiences

Installed SKILL.md frontmatter and owner-skill.json may declare `audiences` as
`[group]`, `[private]` or `[group, private]`. Unknown, duplicate or empty lists
are invalid. An undeclared legacy Skill remains group-only. Owner declarations
can override a legacy Skill's audience, but cannot grant new tools or permissions.

Directory summaries, always-on instructions, automatic matching and skill_load
all filter by the same view. Filtering happens before selection and text budgets.
Voice and meme instruction skills are explicitly shared; existing group-oriented
skills remain group-only. New private workflows can be installed with an
explicit private audience.

Public extension inputs remain Skills and declarative tasks. The interfaces in
this refactor are internal; no runtime tool, graph or code plugin loader exists.

## Quality workflow

Architecture ownership is fixed in `.code-quality-gate/ARCHITECTURE.md`. The initial
contract revisions and evidence are retained. R2 adds the disabled-private-tools
negative case; R3 corrects the ledger assertion and tests JUnit compatibility.
The user instructed continuation after the frozen-test correction was explained;
the revised acceptance was rerun against the pre-refactor baseline before green.
No threshold was lowered. See `.code-quality-gate/REVISION_R3.md`.

Use the committed Code Quality Gate tarball and lockfile:

```sh
npm ci --prefix .code-quality-gate/toolchain --ignore-scripts
node .code-quality-gate/toolchain/node_modules/code-quality-gate/src/cli.mjs status
node .code-quality-gate/toolchain/node_modules/code-quality-gate/src/cli.mjs gate
```

The active R3 manifest selects `quality.config.json`. Tests and
adapters are frozen after red. Every green attempt and final gate produces a
Markdown/JSON report under `.quality/code-quality-gate/`. The gate runs all configured
checks again and validates source hashes. No remote branch protection is claimed.
