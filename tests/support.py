"""Test fixture combining repositories; production has no composite Store."""

from qqbot.infrastructure.storage.activity import ActivityStore
from qqbot.infrastructure.storage.conversation import ConversationStore
from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.infrastructure.storage.jobs import JobsStore
from qqbot.infrastructure.storage.memory import MemoryStore
from qqbot.infrastructure.storage.relationships import RelationshipsStore


class Store:
    def __init__(self, path):
        database = SqliteDatabase(path)
        # Kept so tests can exercise the database-level helpers (migrations,
        # cross-domain deletion) rather than only the repositories.
        self.database = database
        self.db = database.db
        self.conversations = ConversationStore(database)
        self.people = RelationshipsStore(database)
        self.memories = MemoryStore(database)
        self.activity = ActivityStore(database)
        self.jobs = JobsStore(database)

    def __getattr__(self, name):
        for repository in (
            self.conversations,
            self.people,
            self.memories,
            self.activity,
            self.jobs,
        ):
            if hasattr(repository, name):
                return getattr(repository, name)
        raise AttributeError(name)


def bot_policy(allowed_groups, memory_extract_every, timezone, affection_auto_enabled,
               private_enabled=False, job_daily_limit=6, active_start_hour=7,
               active_end_hour=23, job_cooldown_minutes=30, job_freshness_minutes=180,
               job_max_chars=150, **schedule_options):
    """Construct the canonical composite policy for small isolated test setups."""
    from qqbot.contracts.models import BotPolicy, ConversationPolicy, SchedulePolicy
    names = {"job_daily_limit": "daily_limit", "job_cooldown_minutes": "cooldown_minutes",
             "job_freshness_minutes": "freshness_minutes", "job_max_chars": "max_chars"}
    schedule_options = dict(job_daily_limit=job_daily_limit, active_start_hour=active_start_hour,
        active_end_hour=active_end_hour, job_cooldown_minutes=job_cooldown_minutes,
        job_freshness_minutes=job_freshness_minutes, job_max_chars=job_max_chars, **schedule_options)
    return BotPolicy(
        ConversationPolicy(allowed_groups, memory_extract_every, timezone,
                           affection_auto_enabled, private_enabled),
        SchedulePolicy(**{names.get(key, key): value for key, value in schedule_options.items()}),
    )
