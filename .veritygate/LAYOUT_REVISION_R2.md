# Layout task R2: test-module import relocation

R1 established 19 genuine failing layout assertions and no loading errors.
Its frozen inputs and evidence are preserved in history/PROJECT-LAYOUT-004-R1-inputs.zip;
its task/evidence files are retained unchanged. No R1 green workflow attempts were
consumed, so this revision does not reset an exhausted or failed green budget.

After physical migration, the 21 layout tests and six import-linter contracts
passed. Full regression collection found three files still using bare sibling
test-module imports (test_scene_refactor, test_service and test_behavior_experience_runtime).
Those imports are corrected to tests.agent.*. The experiment console command
expectation now names the native tools.experiments module invocation. No business assertions, test names,
thresholds, fixtures or production requirements are weakened. The original
TEST_MIGRATION.json inventory remains authoritative.

R2 freezes the corrected test imports. Its real red run will execute against the
original pre-layout production source in an isolated baseline checkout, with
the relocated acceptance tests and final packaging declaration overlaid. Green
and final gate run against the real migrated checkout; its files are never moved
out of the user's working tree.
The snapshot procedure preserves both versions without claiming that production
was written again after the import-only amendment.

The user requested local-only teaching material. It is stored outside the Git
repository; repository docs describe maintenance ownership and current behavior.
