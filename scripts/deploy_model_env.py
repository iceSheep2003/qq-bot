"""Server-side owner utility: verify and atomically merge three model settings.

Never executes the input file as a shell script. Preserves unrelated settings
and creates a private recoverable backup. Does not restart the bot itself.
"""

from __future__ import annotations
import argparse
import json
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

import httpx

NAMES = {"BOT_MODEL_BASE_URL", "BOT_MODEL_API_KEY", "BOT_MODEL_NAME"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.input.chmod(0o600)
    values = {}
    for line in args.input.read_text().splitlines():
        name, sep, value = line.partition("=")
        if not sep or name not in NAMES or name in values:
            raise SystemExit("Invalid model credential file")
        values[name] = value
    if (
        set(values) != NAMES
        or values["BOT_MODEL_BASE_URL"] != "https://api.deepseek.com/v1"
        or values["BOT_MODEL_NAME"] != "deepseek-flash"
    ):
        raise SystemExit("Unexpected model configuration")
    base = values["BOT_MODEL_BASE_URL"]
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            headers = {"Authorization": "Bearer " + values["BOT_MODEL_API_KEY"]}
            response = client.get(base + "/models", headers=headers)
            if response.status_code != 200 or values["BOT_MODEL_NAME"] not in [
                m.get("id") for m in response.json().get("data", [])
            ]:
                raise SystemExit("Server model discovery failed")
            response = client.post(
                base + "/chat/completions",
                headers=headers,
                json={
                    "model": values["BOT_MODEL_NAME"],
                    "messages": [{"role": "user", "content": "Reply OK."}],
                    "max_tokens": 8,
                    "thinking": {"type": "disabled"},
                },
            )
            if response.status_code != 200 or not response.json().get("choices"):
                raise SystemExit("Server synthetic model probe failed")
        if args.apply:
            old = args.target.read_text()
            backup = args.target.with_name(
                args.target.name + ".before-deepseek-" + str(int(time.time()))
            )
            shutil.copy2(args.target, backup)
            backup.chmod(0o600)
            lines = [
                line
                for line in old.splitlines()
                if not re.match(
                    r"^\s*(?:export\s+)?(?:BOT_MODEL_BASE_URL|BOT_MODEL_API_KEY|BOT_MODEL_NAME)\s*=",
                    line,
                )
            ]
            lines += [name + "=" + shlex.quote(values[name]) for name in sorted(NAMES)]
            fd, path = tempfile.mkstemp(prefix=".model-env-", dir=args.target.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write("\n".join(lines) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(path, 0o600)
                os.replace(path, args.target)
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        print(
            json.dumps(
                {
                    "probe": "passed",
                    "model": values["BOT_MODEL_NAME"],
                    "applied": args.apply,
                    "private_backup": args.apply,
                }
            )
        )
    except httpx.HTTPError:
        raise SystemExit("Server model connection failed; no secrets printed") from None


if __name__ == "__main__":
    main()
