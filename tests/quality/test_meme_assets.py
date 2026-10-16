"""The default meme distribution must not carry Kinna IP artwork."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_default_meme_catalog_contains_no_ip_assets():
    catalog = json.loads((ROOT / "memes" / "catalog.json").read_text(encoding="utf-8"))
    files = [item["file"] for item in catalog.get("memes", [])]
    assert not (ROOT / "memes" / "ip").exists()
    assert not any(path.startswith("ip/") for path in files)


def test_project_docs_do_not_refer_to_removed_ip_pack():
    paths = [
        ROOT / "README.md",
        ROOT / "THIRD_PARTY_NOTICES.md",
        ROOT / "docs" / "guides" / "QUICKSTART.md",
        ROOT / "src" / "qqbot" / "interfaces" / "webui" / "docs" / "GUIDE.md",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "memes/ip" not in text
    assert "zinxtick-present-sheet" not in text
