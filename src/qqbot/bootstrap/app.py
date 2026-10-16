"""Composition root: choose concrete adapters and run the application."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from qqbot.agent.orchestrator import Agent
from qqbot.bootstrap.config import Config
from qqbot.bootstrap.runtime import ApplicationRuntime, Closer, close_sync
from qqbot.interfaces.qq.router import QqEventRouter
from qqbot.contracts.models import BotPolicy, ConfigError, ConversationPolicy, SchedulePolicy
from qqbot.features.loader import (
    bind_features,
    bind_management,
    build_features,
    build_registry,
    build_workers,
    enabled_skills,
    validate_features,
)
from qqbot.infrastructure.adapters.events import parse_message
from qqbot.infrastructure.adapters.model import ModelClient
from qqbot.infrastructure.adapters.structured import InstructorOutput
from qqbot.infrastructure.adapters.perception import configured_perception
from qqbot.infrastructure.adapters.inbound_media import configured_media_source
from qqbot.agent.perception.inbound import InboundMedia
from qqbot.agent.perception.service import PerceptionService
from qqbot.infrastructure.storage.assets import AssetStore
from qqbot.agent.memory.service import MemoryService
from qqbot.agent.identity.guard import IdentityGuard
from qqbot.infrastructure.adapters.onebot import OneBotGateway
from qqbot.agent.memory.relationships.evaluator import AffectionEvaluator
from qqbot.agent.memory.relationships.state import policy_from_env
from qqbot.agent.conversation.intelligence import ConversationIntelligence
from qqbot.actions.replies import (
    CasualMediaPolicy,
    DefaultReplyPlanner,
    HumanizedPacer,
    QQFacePolicy,
    ReplyDispatcher,
)
from qqbot.actions.replies.quotes import QuotePolicy
from qqbot.actions.scheduling import Scheduler
from qqbot.actions.scheduling.runner import JobRunner
from qqbot.actions.scheduling.management import ScheduleManagementService
from qqbot.agent.service import ConversationService
from qqbot.agent.skills.catalog import SkillCatalog
from qqbot.infrastructure.storage.activity import ActivityStore
from qqbot.infrastructure.storage.conversation import ConversationStore
from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.infrastructure.storage.jobs import JobsStore
from qqbot.infrastructure.storage.knowledge import KnowledgeStore
from qqbot.infrastructure.storage.ontology import GraphStore
from qqbot.infrastructure.storage.executions import ExecutionStore
from qqbot.infrastructure.storage.observations import ObservationOutbox
from qqbot.agent.memory.social.projection import SocialProjector
from qqbot.agent.memory.knowledge.service import KnowledgeService
from qqbot.agent.memory.knowledge.extractor import SemanticExtractor
from qqbot.agent.memory.knowledge.coordinator import OntologyMemory
from qqbot.infrastructure.storage.extraction_runs import ExtractionRunStore
from qqbot.agent.conversation.projection import ConversationProjector
from qqbot.actions.tools.kernel import KernelTools
from qqbot.agent.skills.host import OwnerSkills
from qqbot.infrastructure.tool_handlers import BuiltinHandlers
from qqbot.infrastructure.storage.memory import MemoryStore
from qqbot.infrastructure.storage.prompt_sessions import PromptSessionStore
from qqbot.agent.conversation.compaction import ConversationCompactor
from qqbot.actions.interactions import (
    LightInteractionPolicy,
    LightInteractionSettings,
    PokeController,
    PokeSettings,
    ReactionPolicy,
    ReactionSettings,
)
from qqbot.infrastructure.storage.topic_observations import TopicObservationStore
from qqbot.infrastructure.storage.relationships import RelationshipsStore
from qqbot.actions.tools.registry import built_in_tools
from qqbot.agent.cognition.routes import configured_scene_routes
from qqbot.infrastructure.storage.wiki_runtime import configured_wiki_reader
from qqbot.infrastructure.storage.private_chat import PrivateChatQuota

log = logging.getLogger(__name__)

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
            close_sync(self._closers)
            self._closers = []
            raise

    def _assemble(self, config: Config) -> None:
        # Fail before opening anything. A missing token or an impossible quiet
        # window must not leave a database connection and an HTTP client behind
        # just to unwind them again.
        config.validate()
        config.validate_storage_selection()

        database = SqliteDatabase(config.db_path)
        self._closers.append(database.db.close)
        conversations = ConversationStore(database)
        conversations.recover_parts()
        graph = GraphStore(database)
        executions = ExecutionStore(database)
        executions.recover_uncertain()
        ontology = KnowledgeService(graph)
        assets = AssetStore(database, config.db_path.parent / "assets", graph)
        vision, asr = configured_perception()
        for provider in (vision, asr):
            if provider is not None:
                self._closers.append(provider.close)
        perception = PerceptionService(assets, vision=vision, asr=asr)
        projector = ConversationProjector(conversations, graph)
        people = RelationshipsStore(database, policy=policy_from_env())
        memories = MemoryStore(database)
        knowledge = KnowledgeStore(database)
        prompt_sessions = PromptSessionStore(database)
        topic_observations = TopicObservationStore(database)
        activity = ActivityStore(database)
        jobs = JobsStore(database)
        model = ModelClient(
            config.model_base_url,
            config.model_api_key,
            config.model_name,
            reasoning_effort=config.model_reasoning_effort or None,
            thinking_enabled=config.model_thinking_enabled,
            max_completion_tokens=config.model_max_completion_tokens,
            max_concurrency=config.model_max_concurrency,
        )
        self._closers.append(model.close)
        skills = SkillCatalog(config.skills_path, enabled_skills(config))
        legacy_memory = MemoryService(
            model, conversations, memories, knowledge=knowledge
        )
        extraction_runs = ExtractionRunStore(database)
        extraction_runs.recover()
        memory = OntologyMemory(
            ontology,
            SemanticExtractor(ontology, InstructorOutput(model)),
            projector,
            conversations,
            legacy_memory,
            runs=extraction_runs,
        )
        conversation_intelligence = ConversationIntelligence(
            observations=topic_observations,
            window_messages=config.conversation_window_messages,
            window_seconds=config.conversation_window_seconds,
            window_char_budget=config.conversation_window_chars,
        )
        conversation_compactor = ConversationCompactor(
            model, prompt_sessions, conversations
        )
        # Handed to the features before they register, so one that reads
        # memories finds the coordinator there. Read-only: the memory package
        # stays the sole owner of its data.
        features = build_features(
            config, model, built_in_tools(memories), memory=memory
        )
        tool_handlers = BuiltinHandlers(
            conversations,
            ontology,
            skills,
            legacy_tools=features.tools,
            features=features,
            perception=perception,
        )
        available = frozenset(tool_handlers.handlers())
        skills = OwnerSkills(config.skills_path, skills, available)
        tool_handlers.skills = skills
        kernel_tools = KernelTools(
            tool_handlers.handlers(),
            allowed_scopes=frozenset(f"group:{g}" for g in config.group_allowlist),
            executions=executions,
            private_enabled=config.private_enabled,
        )
        # A feature may open an HTTP client (voice, weather). Captured here so a
        # later assembly failure closes it too.
        self._closers.extend(features.closers)
        features.observers.append(
            SocialProjector(graph, people, features.proactive_gate)
        )
        agent = Agent(
            model,
            conversations,
            people,
            memory,
            skills,
            kernel_tools,
            config.persona_path,
            features.context,
            conversation_intelligence=conversation_intelligence,
            prompt_sessions=prompt_sessions,
            conversation_compactor=conversation_compactor,
            behavior_decision=configured_scene_routes(),
            history_char_budget=config.conversation_window_chars,
            history_message_limit=config.conversation_window_messages,
            live_scene_seconds=config.conversation_window_seconds,
            continuity_ttl_seconds=config.conversation_snapshot_ttl_seconds,
            wiki_reader=configured_wiki_reader(config.db_path),
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
        inbound_source = configured_media_source(gateway)
        self._closers.append(inbound_source.close)
        # Handlers under scheduling/handlers/ are discovered here; both config
        # validation and dispatch follow whatever they declare.
        handlers = build_registry(config)
        scheduler = Scheduler(jobs, config.timezone, handlers.actions())
        # A private-only deployment has no scheduled group destinations.
        if config.group_allowlist or config.schedules_path.exists():
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
        media = features.media()
        service = ConversationService(
            agent,
            conversations,
            people,
            activity,
            gateway,
            policy,
            AffectionEvaluator(model, people, policy_from_env()),
            media,
            tuple(features.observers),
            inbound_media=InboundMedia(inbound_source, assets, conversations),
            reply_planner=DefaultReplyPlanner(
                features.reply_capabilities(),
                qq_faces=QQFacePolicy.from_env(),
                quotes=QuotePolicy.from_env(),
                casual_media=CasualMediaPolicy.from_env(),
            ),
            reply_dispatcher=ReplyDispatcher(
                gateway, media, pacer=HumanizedPacer.from_env()
            ),
            conversation_intelligence=conversation_intelligence,
            reply_policy=features.reply_policy,
            turn_guard=IdentityGuard.built_in(model),
            reaction_policy=ReactionPolicy(ReactionSettings.from_env()),
            reaction_sender=gateway,
            light_interaction_policy=LightInteractionPolicy(
                LightInteractionSettings.from_env(), repeat_ledger=activity
            ),
            native_interaction_sender=gateway,
            prompt_sessions=prompt_sessions,
            observation_backlog=config.observer_queue_size,
            observation_workers=config.observer_workers,
            observation_dedupe=config.observer_dedupe,
            observation_store=ObservationOutbox(database),
            private_admission=PrivateChatQuota(database, daily_limit=config.private_daily_user_limit),
        )
        # Last in, first out: the service drains the observation queue on the
        # way down, and those observers write to the database.
        self._closers.append(service.aclose)
        tool_handlers.bind(service)
        # Features that need a runtime collaborator get it here, rather than
        # app.py importing each feature module to wire it by hand.
        job_runner = JobRunner(service, handlers)
        schedule_management = ScheduleManagementService(
            scheduler,
            job_runner.run,
            config.schedules_path,
            config.group_allowlist,
            handlers.suggested_jobs(),
        )
        bind_features(features, service)
        bind_management(features, schedule_management)
        poke_controller = PokeController(
            PokeSettings.from_env(), service, gateway, config.group_allowlist
        )

        gateway.on_event = QqEventRouter(
            service=service, gateway=gateway, allowed_groups=config.group_allowlist,
            private_enabled=config.private_enabled, poke_controller=poke_controller,
            inbound_handlers=features.inbound_handlers, parse_event=parse_message, logger=log,
        )
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
        self.workers = build_workers(config, service, gateway, features) + list(features.workers)

        async def project_ledger():
            while True:
                try:
                    projector.process(40)
                except Exception:
                    log.warning(
                        "Conversation graph projection failed; durable rows retained"
                    )
                await asyncio.sleep(1)

        self._kernel_workers = (project_ledger, service.durable_observation_loop)
        self.features = features
        self.ontology, self.projector = ontology, projector

    async def run(self) -> None:
        runtime = ApplicationRuntime(
            (self.gateway.run, lambda: self.scheduler.loop(self.job_runner.run),
             *self.workers, *self._kernel_workers),
            self._closers,
        )
        await runtime.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kinna bot — skill-driven QQ group and private chat")
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
        config.validate_storage_selection()
    except ConfigError as error:
        raise SystemExit(str(error)) from None
    if args.check:
        database = SqliteDatabase(config.db_path)
        try:
            handlers = build_registry(config)
            scheduler = Scheduler(
                JobsStore(database), config.timezone, handlers.actions()
            )
            if config.group_allowlist or config.schedules_path.exists():
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
