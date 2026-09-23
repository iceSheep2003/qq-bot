"""Test fixture combining repositories; production has no composite Store."""

from qunbot.storage.activity import ActivityStore
from qunbot.storage.conversation import ConversationStore
from qunbot.storage.database import SqliteDatabase
from qunbot.storage.jobs import JobsStore
from qunbot.storage.memory import MemoryStore
from qunbot.storage.relationships import RelationshipsStore


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
