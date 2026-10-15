# Owner selection budget: SKILL-BUDGET-002

Scene-core's gate passed. A manual review then found a remaining boundary:
OwnerSkills removes unavailable/overridden legacy skills after the legacy
catalog has already applied its three-item limit. Three excluded high-priority
skills can therefore suppress a valid fourth skill without leaking its text.

Acceptance covers group/private and override/unavailable cases. The fix belongs
only in runtime/skills.py selection and skills/host.py delegation. Filter those
identities before ranking/limits, preserving owner skills' explicit-load policy.
No graph, tool, task or permission contract changes are required. The existing
scene acceptance, full regression, lint, import and coverage gates remain active.
