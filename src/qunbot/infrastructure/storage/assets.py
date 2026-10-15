"""Private assets and perception cache. Host supplies bytes, not model URLs."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

from qunbot.contracts.schema import ContractError, fingerprint
from qunbot.contracts.ontology.objects import entity, stable_id
from qunbot.infrastructure.storage.base import SqliteRepository


def migrate(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS scoped_assets(
        scope TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,mime TEXT NOT NULL,
        digest TEXT NOT NULL,relative_path TEXT NOT NULL,source_ref TEXT NOT NULL,
        size INTEGER NOT NULL,PRIMARY KEY(scope,id)
      );
      CREATE TABLE IF NOT EXISTS perception_results(
        scope TEXT NOT NULL,cache_key TEXT NOT NULL,payload_json TEXT NOT NULL,
        PRIMARY KEY(scope,cache_key)
      );
    """)


def detect_mime(payload):
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image", "image/gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image", "image/webp"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        return "audio", "audio/wav"
    if payload.startswith(b"OggS"):
        return "audio", "audio/ogg"
    if (
        payload.startswith(b"ID3")
        or len(payload) > 2
        and payload[0] == 255
        and payload[1] & 224 == 224
    ):
        return "audio", "audio/mpeg"
    raise ContractError("Asset has unsupported or unverified format")


class AssetStore(SqliteRepository):
    def __init__(self, database, root, graph):
        super().__init__(database)
        self.root = Path(root).resolve()
        self.graph = graph
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def import_bytes(self, scope, payload, *, source_ref, expected_kind=None, now=0):
        if not isinstance(payload, bytes) or not 0 < len(payload) <= 8 * 1024 * 1024:
            raise ContractError("Asset byte budget exceeded")
        kind, mime = detect_mime(payload)
        if expected_kind and kind != expected_kind:
            raise ContractError("Asset format does not match expected media")
        digest = hashlib.sha256(payload).hexdigest()
        identity = stable_id("asset", scope, digest)
        relative = fingerprint(scope) + "/" + digest
        path = self.root / relative
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if not path.resolve().is_relative_to(self.root):
            raise ContractError("Asset path escapes private root")
        if not path.exists():
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(payload)
        node = self.graph.get(scope, identity) or entity(
            "MediaAsset",
            scope,
            identity,
            now,
            storage_ref=identity,
            kind=kind,
            mime=mime,
            bytes=len(payload),
            duration_ms=None,
            sha256=digest,
            sources=[
                {
                    "kind": "platform",
                    "ref": source_ref,
                }
            ],
        )
        self.graph.write(scope, [node], [], reason="validated-scoped-asset")
        with self.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO scoped_assets VALUES(?,?,?,?,?,?,?,?)",
                (
                    scope,
                    identity,
                    kind,
                    mime,
                    digest,
                    relative,
                    source_ref,
                    len(payload),
                ),
            )
        return node

    def resolve(self, scope, identity, *, kind=None):
        row = self.db.execute(
            "SELECT * FROM scoped_assets WHERE scope=? AND id=?", (scope, identity)
        ).fetchone()
        if not row or kind and row["kind"] != kind:
            raise ContractError("Asset is not allowed in this scope")
        path = (self.root / row["relative_path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ContractError("Asset file unavailable")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row["digest"]:
            raise ContractError("Asset integrity check failed")
        return dict(row), payload

    def cached(self, scope, key):
        row = self.db.execute(
            "SELECT payload_json FROM perception_results WHERE scope=? AND cache_key=?",
            (scope, key),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, scope, key, result):
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > 32768:
            raise ContractError("Perception result exceeds budget")
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO perception_results VALUES(?,?,?)",
                (scope, key, encoded),
            )
