# SCENE-CORE-001 acceptance expansion

The initial red run counted 34 cases against a minimum of 35. It also observed
28 behavior failures, no errors and no skips. No green attempt was consumed.
Its evidence, tests, adapter and quality.config.json remain unchanged.

SCENE-CORE-001-R2 adds one negative requirement: a disabled private channel must
deny tools even for a canonical peer. The new test is written before its
implementation. The revised policy runs both acceptance files and retains all
original thresholds, regression, lint, architecture and coverage checks.
This is a stronger acceptance revision, not a reset of exhausted attempts.

The active task names `.veritygate/quality.scene-core.json`; `veritygate gate`
reads this path from `.veritygate/active.json`. To run an independent audit use
`veritygate audit --config .veritygate/quality.scene-core.json`.
