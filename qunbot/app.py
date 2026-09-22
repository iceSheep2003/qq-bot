"""Composition root: choose concrete adapters and run the application."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from inspect import isawaitable

from .runtime.agent import Agent
from .config import Config
from .extensions.loader import (
    build_features, build_registry, build_workers, enabled_skills, validate_features,
)
from .adapters.events import parse_message
from .adapters.model import ModelClient
from .memory.service import MemoryService
from .adapters.onebot import OneBotGateway
from .relationships.evaluator import AffectionEvaluator
from .scheduling import Scheduler
from .scheduling.runner import JobRunner
from .runtime.service import BotPolicy, ConversationService
from .runtime.skills import SkillCatalog
from .storage.activity import ActivityStore
from .storage.conversation import ConversationStore
from .storage.database import SqliteDatabase
from .storage.jobs import JobsStore
from .storage.memory import MemoryStore
from .storage.relationships import RelationshipsStore
from .runtime.tools import built_in_tools

log = logging.getLogger(__name__)


class BotApp:
    def __init__(self, config: Config):
        database = SqliteDatabase(config.db_path)
        conversations = ConversationStore(database)
        people = RelationshipsStore(database)
        memories = MemoryStore(database)
        activity = ActivityStore(database)
        jobs = JobsStore(database)
        model = ModelClient(
            config.model_base_url, config.model_api_key, config.model_name
        )
        skills = SkillCatalog(config.skills_path, enabled_skills(config))
        features = build_features(config, model, built_in_tools(memories))
        agent = Agent(
            model,
            conversations,
            people,
            MemoryService(model, conversations, memories),
            skills,
            features.tools,
            config.persona_path,
            features.context,
        )
        gateway = OneBotGateway(
            config.onebot_host,
            config.onebot_port,
            config.onebot_token,
            inbound_backlog=config.onebot_inbound_backlog,
            max_lanes=config.onebot_max_lanes,
            request_timeout=config.onebot_request_timeout,
            max_frame_bytes=config.onebot_max_frame_kb * 1024,
        )
        # Handlers under scheduling/handlers/ are discovered here; both config
        # validation and dispatch follow whatever they declare.
        handlers = build_registry(config)
        scheduler = Scheduler(jobs, config.timezone, handlers.actions())
        scheduler.sync_config(
            config.schedules_path, config.group_allowlist, handlers.suggested_jobs()
        )
        policy = BotPolicy(
            config.group_allowlist,
            config.memory_extract_every,
            config.timezone,
            config.affection_auto_enabled,
            config.private_enabled,
            config.job_daily_limit,
            config.active_start_hour,
            config.active_end_hour,
            config.job_cooldown_minutes,
            config.job_freshness_minutes,
            config.job_max_chars,
        )
        service = ConversationService(
            agent,
            conversations,
            people,
            activity,
            gateway,
            policy,
            AffectionEvaluator(model, people),
            features.media(),
            tuple(features.observers),
            reply_policy=features.reply_policy,
            observation_backlog=config.observer_queue_size,
            observation_workers=config.observer_workers,
            observation_dedupe=config.observer_dedupe,
        )
        job_runner = JobRunner(service, handlers)

        async def on_event(raw: dict) -> None:
            event = parse_message(raw)
            if event:
                try:
                    await service.handle_message(event)
                except Exception:
                    log.exception("Failed to handle QQ event %s", event.event_id)

        gateway.on_event = on_event
        self.gateway, self.scheduler, self.job_runner, self.model = (
            gateway,
            scheduler,
            job_runner,
            model,
        )
        self.config = config
        self.database = database
        # Feature-declared loops run beside the scheduled-job workers. A feature
        # loop that raises is caught by the gather below and reports itself
        # rather than taking the gateway down with it.
        self.workers = build_workers(config, service, gateway, features) + [
            worker() for worker in features.workers
        ]
        self.features = features

    async def run(self) -> None:
        try:
            await asyncio.gather(
                self.gateway.run(),
                self.scheduler.loop(self.job_runner.run),
                *self.workers,
            )
        finally:
            # Drain queued observations *before* closing the database — they
            # write to it. Skipping this drops in-flight affection/mood updates
            # on every restart instead of merely on a crash.
            await self.service.aclose()
            await self.model.close()
            self.database.db.close()
            for close in reversed(self.features.closers):
                result = close()
                if isawaitable(result):
                    await result


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
    if args.check:
        database = SqliteDatabase(config.db_path)
        handlers = build_registry(config)
        scheduler = Scheduler(JobsStore(database), config.timezone, handlers.actions())
        scheduler.sync_config(
            config.schedules_path, config.group_allowlist, handlers.suggested_jobs()
        )
        jobs = [
            job
            for group_id in sorted(config.group_allowlist)
            for job in scheduler.list(group_id)
        ]
        running = [job for job in jobs if job["enabled"]]
        config.persona_path.read_text(encoding="utf-8")
        feature_status = validate_features(config)
        print(
            json.dumps(
                {
                    "model": config.model_name,
                    "groups": sorted(config.group_allowlist),
                    "skills": [
                        s.name for s in SkillCatalog(config.skills_path, enabled_skills(config)).skills
                    ],
                    "actions": sorted(handlers.actions()),
                    "extensions": sorted(config.extensions),
                    "enabled_jobs": [
                        {"id": job["config_key"], "action": job["action"]}
                        for job in running
                    ],
                    # Handler suggestions, seeded disabled. Paste a block into
                    # config/schedules.json to switch that job on.
                    "to_enable": [
                        {
                            "id": job["config_key"],
                            "group_id": job["group_id"],
                            "kind": job["schedule_kind"],
                            "value": job["schedule_value"],
                            "action": job["action"],
                            "prompt": job["prompt"],
                        }
                        for job in jobs
                        if not job["enabled"]
                    ],
                    "features": feature_status,
                },
                ensure_ascii=False,
            )
        )
        database.db.close()
        return
    asyncio.run(BotApp(config).run())


if __name__ == "__main__":
    main()
