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
