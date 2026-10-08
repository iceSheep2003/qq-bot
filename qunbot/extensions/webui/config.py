from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WebUIConfig:
    enabled: bool
    host: str
    port: int
    token: str
    env_path: Path
    # The console edits the learning review file in place rather than holding
    # its own copy of the decisions, so it has to point at the same file the
    # extension reads. Same variable, same default.
    review_path: Path = Path("./config/slang_review.json")
    persona_path: Path = Path("./config/persona.md")

    @classmethod
    def from_env(cls) -> "WebUIConfig":
        return cls(
            enabled=os.getenv("BOT_WEBUI_ENABLED", "false").lower() == "true",
            host=os.getenv("BOT_WEBUI_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("BOT_WEBUI_PORT", "6200")),
            token=os.getenv("BOT_WEBUI_TOKEN", ""),
            env_path=Path(os.getenv("BOT_WEBUI_ENV_PATH", ".env")),
            review_path=Path(
                os.getenv("BOT_SLANG_REVIEW_PATH", "./config/slang_review.json")
            ),
            persona_path=Path(os.getenv("BOT_PERSONA_PATH", "./config/persona.md")),
        )

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("BOT_WEBUI_PORT must be between 1 and 65535")
        if self.enabled and not self.token:
            raise ValueError("BOT_WEBUI_TOKEN is required when WebUI is enabled")
        if self.enabled and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("WebUI only permits loopback binding; use a trusted reverse proxy for remote access")
