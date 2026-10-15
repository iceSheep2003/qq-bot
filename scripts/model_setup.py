"""Owner-only CCSwitch credential import. Never prints secret values.

Use --probe before --export. Generated files are private deployment artifacts,
not source files. Only the explicitly selected official DeepSeek profile is read.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def deepseek_credentials(database: Path) -> tuple[str, str]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        rows = db.execute(
            "SELECT settings_config FROM providers WHERE app_type='claude' AND lower(name)='deepseek'"
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("Expected one explicit DeepSeek profile")
    env = json.loads(rows[0][0])["env"]
    if urlsplit(env.get("ANTHROPIC_BASE_URL", "")).hostname != "api.deepseek.com":
        raise ValueError("The selected profile is not the official DeepSeek endpoint")
    key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        raise ValueError("DeepSeek credential unavailable or malformed")
    return "https://api.deepseek.com/v1", key


def probe(base: str, key: str) -> tuple[list[str], str]:
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get(
            base + "/models", headers={"Authorization": "Bearer " + key}
        )
        if response.status_code != 200:
            raise ValueError(f"DeepSeek model discovery HTTP {response.status_code}")
        ids = sorted(
            m["id"]
            for m in response.json().get("data", [])
            if isinstance(m.get("id"), str)
        )
        print(json.dumps({"discovered_models": ids}))
        candidates = [
            m
            for m in ids
            if (m == "deepseek-flash" or "v4" in m.lower())
            and "flash" in m.lower()
            and "expires" not in m.lower()
        ]
        if not candidates:
            raise ValueError(
                "Requested V4 Flash is absent from authenticated model discovery"
            )
        model = (
            "deepseek-v4-flash" if "deepseek-v4-flash" in candidates else candidates[0]
        )
        # Synthetic probe only: no QQ records, no private prompt.
        response = client.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
                "thinking": {"type": "disabled"},
            },
        )
        if response.status_code != 200 or not response.json().get("choices"):
            raise ValueError(f"DeepSeek completion probe HTTP {response.status_code}")
        return ids, model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ccswitch", type=Path, default=Path.home() / ".cc-switch/cc-switch.db"
    )
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        base, key = deepseek_credentials(args.ccswitch)
        ids, model = probe(base, key)
        if args.export:
            args.export.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with args.export.open("x", encoding="utf-8") as stream:
                os.chmod(args.export, 0o600)
                stream.write(
                    f"BOT_MODEL_BASE_URL={base}\nBOT_MODEL_API_KEY={key}\nBOT_MODEL_NAME={model}\n"
                )
        print(
            json.dumps(
                {
                    "endpoint": base,
                    "available_models": ids,
                    "selected_model": model,
                    "probe": "passed",
                    "exported": bool(args.export),
                }
            )
        )
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except (KeyError, httpx.HTTPError, sqlite3.Error):
        # Do not print response bodies, request headers or connection URLs.
        raise SystemExit(
            "DeepSeek discovery/probe failed; no credential exported"
        ) from None


if __name__ == "__main__":
    main()
