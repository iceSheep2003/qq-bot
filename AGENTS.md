<!-- veritygate:start -->
## VerityGate code-change workflow
For code changes, read .agents/skills/veritygate/SKILL.md before editing implementation. Write acceptance tests, record a real red phase, implement and rerun green until all required checks pass, then run veritygate gate before completion or any authorized merge. Keep tests, thresholds, adapters and evidence intact. Respect the user's scope and stop request. Reports are saved in .quality/veritygate/.
<!-- veritygate:end -->
