"""Generate a local audition set from the 40-line group-chat voice corpus."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

from qunbot.extensions.voice.sources import CosyVoiceSpeechSource, QwenAudioSpeechSource


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("data/voice_eval/group_chat_40.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("outputs/tts-auditions/group-chat-40"))
    parser.add_argument("--provider", choices=("cosyvoice", "qwen_audio"), default="cosyvoice")
    parser.add_argument("--model")
    parser.add_argument("--voice")
    args = parser.parse_args()
    settings = {}
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("BOT_TTS_") and "=" in line:
            key, value = line.split("=", 1)
            settings[key] = value.strip().strip('"').strip("'")
    defaults = {
        "cosyvoice": ("cosyvoice-v3-flash", "longfeifei_v3", CosyVoiceSpeechSource),
        "qwen_audio": ("qwen-audio-3.0-tts-flash", "longanfengyue", QwenAudioSpeechSource),
    }
    model, voice, source_type = defaults[args.provider]
    source = source_type(settings["BOT_TTS_BASE_URL"], settings["BOT_TTS_API_KEY"], args.model or model, args.voice or voice)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = []
    rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    try:
        for row in rows:
            styled = getattr(source, "synthesize_styled", None)
            audio = await styled(row["text"], row["style"]) if styled else await source.synthesize(row["text"])
            path = args.output / f"{row['id']}.wav"
            path.write_bytes(base64.b64decode(audio.removeprefix("base64://")))
            manifest.append({**row, "audio": str(path)})
            print(row["id"], row["source"], path)
    finally:
        await source.close()
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
