"""Local, credited campus assets for the optional daily poster action."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CATALOG = Path(__file__).with_name("campuses") / "catalog.json"


@dataclass(frozen=True)
class Campus:
    id: str
    name: str
    city: str
    scene: str
    photo: Path
    logo: Path
    photographer: str
    license: str
    source: str


def load_campuses(path: Path = DEFAULT_CATALOG) -> tuple[Campus, ...]:
    """Load only files beneath the catalog directory; fail during startup.

    A missing photo must not quietly turn the scheduled card into a text-only
    message at 07:00. The operator can replace or add schools by editing the
    catalog and adding local assets, then running ``qunbot --check``.
    """
    path = Path(path)
    root = path.parent.resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("campuses"), list):
        raise ValueError("campus catalog must contain a campuses list")
    campuses: list[Campus] = []
    seen: set[str] = set()
    for item in raw["campuses"]:
        if not isinstance(item, dict):
            raise ValueError("campus catalog entries must be objects")
        fields = ("id", "name", "city", "scene", "photo", "logo", "photographer", "license", "source")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in fields):
            raise ValueError("campus catalog entry has a missing or empty field")
        if item["id"] in seen:
            raise ValueError(f"duplicate campus id: {item['id']}")
        seen.add(item["id"])

        def asset(key: str) -> Path:
            candidate = (root / item[key]).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                raise ValueError(f"campus {item['id']} has an invalid {key} asset")
            return candidate

        if not item["source"].startswith("https://commons.wikimedia.org/wiki/File:"):
            raise ValueError(f"campus {item['id']} needs a Wikimedia Commons source page")
        campuses.append(
            Campus(
                id=item["id"],
                name=item["name"],
                city=item["city"],
                scene=item["scene"],
                photo=asset("photo"),
                logo=asset("logo"),
                photographer=item["photographer"],
                license=item["license"],
                source=item["source"],
            )
        )
    if len(campuses) < 2:
        raise ValueError("campus poster needs at least two different universities")
    return tuple(campuses)
