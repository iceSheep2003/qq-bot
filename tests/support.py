"""Test fixture combining repositories; production has no composite Store."""

from qunbot.infrastructure.storage.activity import ActivityStore
from qunbot.infrastructure.storage.conversation import ConversationStore
from qunbot.infrastructure.storage.database import SqliteDatabase
from qunbot.infrastructure.storage.jobs import JobsStore
from qunbot.infrastructure.storage.memory import MemoryStore
from qunbot.infrastructure.storage.relationships import RelationshipsStore


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
