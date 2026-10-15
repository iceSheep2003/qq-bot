<!-- code-quality-gate:start -->
## Code Quality Gate code-change workflow
For code changes, read .agents/skills/code-quality-gate/SKILL.md before editing implementation. Write acceptance tests, record a real red phase, implement and rerun green until all required checks pass, then run cqg gate before completion or any authorized merge. Keep tests, thresholds, adapters and evidence intact. Respect the user's scope and stop request. Reports are saved in .quality/code-quality-gate/. After each implementation iteration, supply a short --note and read history.md to compare measured metrics; never edit historical numeric facts.
<!-- code-quality-gate:end -->
