"""Audition the configured TTS; optionally send one sample to an allowlisted QQ group.

Run from the repository root. Sending is opt-in and never retried automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qunbot.extensions.voice.sources import (  # noqa: E402
    CosyVoiceSpeechSource, DashScopeSpeechSource, HttpSpeechSource,
    QwenAudioSpeechSource,
)


PROVIDERS = {
    "cosyvoice": CosyVoiceSpeechSource,
    "dashscope": DashScopeSpeechSource,
    "openai": HttpSpeechSource,
    "qwen_audio": QwenAudioSpeechSource,
}


def load_env(path: Path) -> dict[str, str]:
    settings = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("BOT_TTS_") or key == "BOT_GROUP_ALLOWLIST":
            settings[key] = value.strip().strip('"').strip("'")
    return settings


def group_is_allowed(group_id: int, allowlist: str) -> bool:
    return str(group_id) in {item.strip() for item in allowlist.replace(";", ",").split(",")}


async def send_once(wav: bytes, *, group_id: int, napcat_config: Path) -> int:
    """One send call only; the returned message ID is required to count success."""
    webui = json.loads((napcat_config / "webui.json").read_text(encoding="utf-8"))
    key_hash = hashlib.sha256((webui["token"] + ".napcat").encode()).hexdigest()
    sample_name = f"voice_smoke_{uuid.uuid4().hex}.wav"
    sample_path = napcat_config / sample_name
    sample_path.write_bytes(wav)
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            login = await client.post(
                "http://127.0.0.1:6099/api/auth/login", json={"hash": key_hash}
            )
            login.raise_for_status()
            credential = login.json()["data"]["Credential"]
            created = await client.post(
                "http://127.0.0.1:6099/api/Debug/create",
                headers={"Authorization": f"Bearer {credential}"}, json={},
            )
            created.raise_for_status()
            if created.json().get("code") != 0:
                raise RuntimeError("NapCat debug adapter unavailable")
            response = await client.post(
                "http://127.0.0.1:6099/api/Debug/call",
                headers={"Authorization": f"Bearer {credential}"},
                json={"action": "send_group_msg", "params": {
                    "group_id": group_id,
                    "message": [{"type": "record", "data": {
                        "file": f"file:///app/napcat/config/{sample_name}"
                    }}],
                }},
            )
            response.raise_for_status()
            result = response.json()
            outcome = result.get("data") or {}
            if result.get("code") != 0 or outcome.get("retcode") != 0:
                raise RuntimeError("NapCat did not confirm the voice message; inspect history before retrying")
            message_id = (outcome.get("data") or {}).get("message_id")
            if not isinstance(message_id, int):
                raise RuntimeError("Send outcome ambiguous; inspect group history before retrying")
            return message_id
    finally:
        sample_path.unlink(missing_ok=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--text", default="今天能坐下来学一会儿，就已经算赢一小步了。")
    parser.add_argument("--style", choices=("neutral", "warm", "playful", "excited", "serious"), default="warm")
    parser.add_argument("--provider", choices=tuple(PROVIDERS))
    parser.add_argument("--model")
    parser.add_argument("--voice")
    parser.add_argument("--output", type=Path, default=Path("voice_smoke.wav"))
    parser.add_argument("--send", action="store_true", help="explicitly send one sample through local NapCat")
    parser.add_argument("--group", type=int)
    parser.add_argument("--napcat-config", type=Path, default=Path("/opt/napcat/config"))
    args = parser.parse_args()
    settings = load_env(args.env)
    if args.send and (not args.group or not group_is_allowed(args.group, settings.get("BOT_GROUP_ALLOWLIST", ""))):
        parser.error("--send requires --group in BOT_GROUP_ALLOWLIST")
    provider = args.provider or settings.get("BOT_TTS_PROVIDER", "cosyvoice")
    source = PROVIDERS[provider](
        settings["BOT_TTS_BASE_URL"], settings["BOT_TTS_API_KEY"],
        args.model or settings["BOT_TTS_MODEL"], args.voice or settings["BOT_TTS_VOICE"],
    )
    try:
        styled = getattr(source, "synthesize_styled", None)
        audio = await styled(args.text, args.style) if styled else await source.synthesize(args.text)
    finally:
        await source.close()
    wav = base64.b64decode(audio.removeprefix("base64://"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(wav)
    print(f"saved {args.output} ({len(wav)} bytes; {provider}/{source.voice})")
    if args.send:
        message_id = await send_once(wav, group_id=args.group, napcat_config=args.napcat_config)
        print(f"NapCat confirmed group={args.group} message_id={message_id}")


if __name__ == "__main__":
    asyncio.run(main())
