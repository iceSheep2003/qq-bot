"""Composition root: choose concrete adapters and run the application."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .agent import Agent
from .config import Config
from .context import ContextRegistry
from .events import parse_message
from .media import ReplyMediaProcessor
from .media_adapters import DashScopeSpeechSource, HttpSpeechSource, LocalMemeCatalog
from .model import ModelClient
from .onebot import OneBotGateway
from .poster import ExamCountdownPoster
from .relationships import AffectionEvaluator
from .scheduler import Scheduler
from .service import BotPolicy, ConversationService
from .skills import SkillCatalog
from .store import Store
from .tools import built_in_tools

log = logging.getLogger(__name__)


class BotApp:
    def __init__(self, config: Config):
        store = Store(config.db_path)
        model = ModelClient(
            config.model_base_url, config.model_api_key, config.model_name
        )
        skills = SkillCatalog(config.skills_path)
        memes = LocalMemeCatalog(config.memes_path)
        context = ContextRegistry()
        context.register("available_meme_tags", lambda _event: memes.available_tags())
        agent = Agent(
            model,
            store,
            store,
            store,
            skills,
            built_in_tools(store),
            config.persona_path,
            context,
        )
        gateway = OneBotGateway(
            config.onebot_host, config.onebot_port, config.onebot_token
        )
        scheduler = Scheduler(store, config.timezone)
        scheduler.sync_config(config.schedules_path, config.group_allowlist)
        speech = None
        if config.tts_enabled and all(
            (
                config.tts_base_url,
                config.tts_api_key,
                config.tts_model,
                config.tts_voice,
            )
        ):
            speech_type = (
                DashScopeSpeechSource
                if config.tts_provider == "dashscope"
                else HttpSpeechSource
            )
            speech = speech_type(
                config.tts_base_url,
                config.tts_api_key,
                config.tts_model,
                config.tts_voice,
            )
        media = ReplyMediaProcessor(memes, speech)
        posters = (
            ExamCountdownPoster(config.exam_date, config.poster_font)
            if config.exam_date
            else None
        )
        policy = BotPolicy(
            config.group_allowlist,
            config.memory_extract_every,
            config.proactive_enabled,
            config.proactive_interval_minutes,
            config.proactive_daily_limit,
            config.timezone,
            config.affection_auto_enabled,
            config.private_enabled,
            config.job_daily_limit,
            config.active_start_hour,
            config.active_end_hour,
            config.job_cooldown_minutes,
            config.job_freshness_minutes,
        )
        service = ConversationService(
            agent,
            store,
            store,
            gateway,
            policy,
            AffectionEvaluator(model, store),
            media,
            posters,
        )

        async def on_event(raw: dict) -> None:
            event = parse_message(raw)
            if event:
                try:
                    await service.handle_message(event)
                except Exception:
                    log.exception("Failed to handle QQ event %s", event.event_id)

        gateway.on_event = on_event
        self.gateway, self.scheduler, self.service, self.model = (
            gateway,
            scheduler,
            service,
            model,
        )
        self.config = config
        self.speech = speech

    async def proactive_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            if not self.gateway.connection:
                continue
            for group_id in self.config.group_allowlist:
                try:
                    await self.service.maybe_proactive(group_id)
                except Exception:
                    log.exception("Proactive check failed for group %s", group_id)

    async def run(self) -> None:
        try:
            await asyncio.gather(
                self.gateway.run(),
                self.proactive_loop(),
                self.scheduler.loop(self.service.run_job),
            )
        finally:
            await self.model.close()
            if self.speech:
                await self.speech.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill-driven QQ group bot")
    parser.add_argument(
        "--check", action="store_true", help="validate configuration without connecting"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = Config.from_env()
    if not config.onebot_token:
        raise SystemExit("BOT_ONEBOT_TOKEN is required")
    if not config.group_allowlist:
        raise SystemExit("BOT_GROUP_ALLOWLIST is required")
    if not config.model_api_key and not args.check:
        raise SystemExit("BOT_MODEL_API_KEY is required")
    tts_fields = (
        config.tts_base_url,
        config.tts_api_key,
        config.tts_model,
        config.tts_voice,
    )
    if config.tts_enabled and not all(tts_fields):
        raise SystemExit("all BOT_TTS_* settings are required when TTS is enabled")
    if config.tts_provider not in {"openai", "dashscope"}:
        raise SystemExit("BOT_TTS_PROVIDER must be openai or dashscope")
    if args.check:
        store = Store(config.db_path)
        scheduler = Scheduler(store, config.timezone)
        scheduler.sync_config(config.schedules_path, config.group_allowlist)
        poster_jobs = [
            job
            for group_id in config.group_allowlist
            for job in scheduler.list(group_id)
            if job["action"] == "poster"
        ]
        if poster_jobs and not config.exam_date:
            raise SystemExit("BOT_EXAM_DATE is required by poster jobs")
        if config.exam_date:
            # Surfaces a missing or unreadable CJK font before the bot runs.
            ExamCountdownPoster(config.exam_date, config.poster_font)
        config.persona_path.read_text(encoding="utf-8")
        LocalMemeCatalog(config.memes_path)
        print(
            json.dumps(
                {
                    "model": config.model_name,
                    "groups": sorted(config.group_allowlist),
                    "skills": [s.name for s in SkillCatalog(config.skills_path).skills],
                    "jobs": [job["config_key"] for job in poster_jobs],
                    "exam_date": str(config.exam_date) if config.exam_date else None,
                },
                ensure_ascii=False,
            )
        )
        return
    asyncio.run(BotApp(config).run())


if __name__ == "__main__":
    main()
