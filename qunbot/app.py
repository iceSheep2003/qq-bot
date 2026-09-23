"""Composition root: choose concrete adapters and run the application."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from inspect import isawaitable
from typing import Any, Callable

from .runtime.agent import Agent
from .config import Config
from .domain import BotPolicy, ConfigError, ConversationPolicy, SchedulePolicy
from .extensions.loader import (
    bind_features,
    build_features,
    build_registry,
    build_workers,
    enabled_skills,
    validate_features,
)
from .adapters.events import parse_message
from .adapters.model import ModelClient
from .memory.service import MemoryService
from .adapters.onebot import OneBotGateway
from .relationships.evaluator import AffectionEvaluator
from .scheduling import Scheduler
from .scheduling.runner import JobRunner
from .runtime.service import ConversationService
from .runtime.skills import SkillCatalog
from .storage.activity import ActivityStore
from .storage.conversation import ConversationStore
from .storage.database import SqliteDatabase
from .storage.jobs import JobsStore
from .storage.memory import MemoryStore
from .storage.relationships import RelationshipsStore
from .runtime.tools import built_in_tools

log = logging.getLogger(__name__)

Closer = Callable[[], Any]
"""A teardown step. May be sync or return an awaitable."""


def _run_awaitable(awaitable: Any) -> None:
    """Settle an async teardown from a synchronous context.

    A failed ``BotApp.__init__`` cannot await: it is called as
    ``asyncio.run(BotApp(config).run())``, so its constructor runs *outside*
    the loop. The usual case is no running loop at all, which ``asyncio.run``
    handles; if a caller did construct inside a loop, the step is scheduled and
    this is best-effort rather than silently dropped.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(awaitable)
        return
    asyncio.ensure_future(awaitable)


def _close_sync(closers: list[Closer]) -> None:
    """Reverse-order teardown that never raises, for a constructor rollback."""
    for close in reversed(closers):
        try:
            result = close()
        except Exception:
            log.exception("Cleanup step failed while unwinding a failed assembly")
            continue
        if isawaitable(result):
            try:
                _run_awaitable(result)
            except Exception:
                log.exception("Async cleanup step failed while unwinding")


async def _aclose_all(closers: list[Closer]) -> None:
    """The same reverse-order teardown, awaited. Used by ``BotApp.run``."""
    for close in reversed(closers):
        try:
            result = close()
            if isawaitable(result):
                await result
        except Exception:
            log.exception("Cleanup step failed during shutdown")


class BotApp:
    def __init__(self, config: Config):
        self.config = config
        # Reverse-order teardown, appended in construction order. Reversing it
        # is what guarantees the service drains queued observations *before* the
        # database closes — they write to it — and that the HTTP clients close
        # before nothing needs them.
        self._closers: list[Closer] = []
        try:
            self._assemble(config)
        except BaseException:
            _close_sync(self._closers)
            self._closers = []
            raise

    def _assemble(self, config: Config) -> None:
        # Fail before opening anything. A missing token or an impossible quiet
        # window must not leave a database connection and an HTTP client behind
        # just to unwind them again.
        config.validate()

        database = SqliteDatabase(config.db_path)
        self._closers.append(database.db.close)
        conversations = ConversationStore(database)
        people = RelationshipsStore(database)
        memories = MemoryStore(database)
        activity = ActivityStore(database)
        jobs = JobsStore(database)
        model = ModelClient(
            config.model_base_url,
            config.model_api_key,
            config.model_name,
            reasoning_effort=config.model_reasoning_effort or None,
        )
        self._closers.append(model.close)
        skills = SkillCatalog(config.skills_path, enabled_skills(config))
        memory = MemoryService(model, conversations, memories)
        # Handed to the features before they register, so one that reads
        # memories finds the coordinator there. Read-only: the memory package
        # stays the sole owner of its data.
        features = build_features(
            config, model, built_in_tools(memories), memory=memory
        )
        # A feature may open an HTTP client (voice, weather). Captured here so a
        # later assembly failure closes it too.
        self._closers.extend(features.closers)
        agent = Agent(
            model,
            conversations,
            people,
            memory,
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
            conversation=ConversationPolicy(
                allowed_groups=config.group_allowlist,
                memory_extract_every=config.memory_extract_every,
                timezone=config.timezone,
                affection_auto_enabled=config.affection_auto_enabled,
                private_enabled=config.private_enabled,
            ),
            schedule=SchedulePolicy(
                daily_limit=config.job_daily_limit,
                active_start_hour=config.active_start_hour,
                active_end_hour=config.active_end_hour,
                cooldown_minutes=config.job_cooldown_minutes,
                freshness_minutes=config.job_freshness_minutes,
                max_chars=config.job_max_chars,
            ),
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
        # Last in, first out: the service drains the observation queue on the
        # way down, and those observers write to the database.
        self._closers.append(service.aclose)
        # Features that need a runtime collaborator get it here, rather than
        # app.py importing each feature module to wire it by hand.
        bind_features(features, service)
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
        # Held so run() can drain the observation queue on shutdown.
        self.service = service
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
            # Reverse order, appended at construction: drain the observation
            # queue before closing the database — they write to it. Skipping
            # this drops in-flight affection/mood updates on every restart
            # instead of merely on a crash. One step failing must not strand
            # the rest, so each is isolated inside _aclose_all.
            await _aclose_all(self._closers)


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
    try:
        # --check exists to be run before the model key has been handed over.
        config.validate(require_model_key=not args.check)
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    if args.check:
        database = SqliteDatabase(config.db_path)
        try:
            handlers = build_registry(config)
            scheduler = Scheduler(
                JobsStore(database), config.timezone, handlers.actions()
            )
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
                        "config_version": config.config_version,
                        "model": config.model_name,
                        "groups": sorted(config.group_allowlist),
                        "skills": [
                            s.name
                            for s in SkillCatalog(
                                config.skills_path, enabled_skills(config)
                            ).skills
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
        finally:
            database.db.close()
        return
    asyncio.run(BotApp(config).run())


if __name__ == "__main__":
    main()
