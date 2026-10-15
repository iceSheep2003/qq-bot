"""Closed audience declarations and scene-specific catalog views.

Legacy instructions remain group-only. A view is text selection, never a new
tool registration or an authorization grant.
"""

AUDIENCES = frozenset({"group", "private"})


def require_audience(value):
    if value not in AUDIENCES:
        raise ValueError("Unknown skill audience")
    return value


def audiences_for(value=None):
    if value is None:
        return frozenset({"group"})
    if isinstance(value, str):
        value = [v.strip().strip("\"'") for v in value.strip().strip("[]").split(",")]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("Skill audiences must be a nonempty list")
    values = [require_audience(v) for v in value]
    if len(set(values)) != len(values):
        raise ValueError("Duplicate skill audience")
    return frozenset(values)


def scoped_skills(catalog, audience):
    """Keep minimal legacy/test ports usable while production uses views."""
    factory = getattr(catalog, "for_audience", None)
    return factory(audience) if callable(factory) else catalog


class ScopedSkills:
    def __init__(self, catalog, audience):
        self.catalog = catalog
        self.audience = require_audience(audience)

    @property
    def skills(self):
        return [s for s in self.catalog.skills if self.audience in s.audiences]

    def by_name(self, name):
        return next((s for s in self.skills if s.name == name), None)

    def select(self, text, *, proactive=False):
        return self.catalog.select(text, proactive=proactive, audience=self.audience)

    def stable_instructions(self):
        return self.catalog.stable_instructions(audience=self.audience)

    def catalog_text(self):
        return self.catalog.catalog_text(audience=self.audience)

    def load(self, name):
        return self.catalog.load(name, audience=self.audience)
