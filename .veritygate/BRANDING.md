# Kinna bot public identity

Scope: rename public product labels in README, package description, CLI help,
WebUI and its guide to Kinna bot. Keep import package, existing qunbot entry point,
environment keys, storage names and protocol identifiers compatible.

Ownership: app.py owns CLI description; static/index.html owns console labels;
pyproject.toml owns package description; README and GUIDE own documentation.
No conversation routing, memory or graph behavior changes in this task.

Before implementation, acceptance checks CLI help with no model configuration,
visible console labels, documentation identity, and legacy entry point presence.
Existing full regression, architecture and coverage checks stay enabled.
A VS Code workspace and code reading map document the actual partial refactor.
