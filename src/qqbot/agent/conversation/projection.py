"""Replayable projection outbox -> fixed graph, shared by live and backfill.

The SQL adapter owns durable source rows and cursor. This service owns mapping
semantics. Graph writes are idempotent; acknowledgement occurs AFTER commit.
"""

from __future__ import annotations
import json

from qqbot.contracts.ontology.objects import edge, entity, ref, stable_id
from qqbot.contracts.schema import ContractError


class ConversationProjector:
    def __init__(self, source, graph):
        self.source, self.graph = source, graph

    def process(self, limit=50):
        count = 0
        rows = self.source.projection_batch(limit)
        for row in rows:
            try:
                self._project(row)
                self.source.ack_projection(row["event_id"])
                count += 1
            except (ContractError, ValueError, TypeError):
                self.source.fail_projection(row["event_id"])
        return count

    def _project(self, row):
        scope = row["scope"]
        stamp = row["event_time"] or row["created_at"] * 1000
        if row["origin"] == "operator":
            return
        group_id = scope.split(":", 1)[-1]
        group_identity = stable_id("group", scope)
        group = self.graph.get(scope, group_identity) or entity(
            "Group",
            scope,
            group_identity,
            stamp,
            platform="qq",
            platform_group_id=group_id,
            allowed=True,
        )
        person_id = stable_id(
            "agent" if row["role"] == "assistant" else "person", scope, row["user_id"]
        )
        person = self.graph.get(scope, person_id)
        if person is None:
            person = (
                entity(
                    "AgentIdentity",
                    scope,
                    person_id,
                    stamp,
                    deployment_id="qqbot",
                    persona_version="owner-persona-v1",
                )
                if row["role"] == "assistant"
                else entity(
                    "Person",
                    scope,
                    person_id,
                    stamp,
                    platform="qq",
                    account_ref=str(row["user_id"]),
                )
            )
        message_id = stable_id("message", scope, row["event_id"])
        source = {"kind": "platform", "ref": row["event_id"]}
        parts = json.loads(row["parts_json"] or "[]")
        safe_parts = []
        media_nodes = []
        for part in parts:
            if part.get("kind") == "text" and len(str(part.get("text") or "")) <= 12000:
                safe_parts.append({"kind": "text", "text": part["text"]})
            elif part.get("kind") in {"image", "audio"} and isinstance(
                part.get("asset_ref"), dict
            ):
                asset_ref = part["asset_ref"]
                asset = self.graph.get(scope, asset_ref.get("id", ""))
                if (
                    asset
                    and asset["type"] == "MediaAsset"
                    and asset["kind"] == part["kind"]
                    and asset_ref == ref(asset)
                ):
                    safe_parts.append({"kind": part["kind"], "asset_ref": ref(asset)})
                    media_nodes.append(asset)
                else:
                    safe_parts.append({"kind": "unknown", "source_ref": source})
            elif (
                part.get("kind") == "face"
                and isinstance(part.get("face_id"), str)
                and 0 < len(part["face_id"]) <= 160
            ):
                safe_parts.append({"kind": "face", "face_id": part["face_id"]})
            elif part.get("kind") == "at" and part.get("all_members") is True:
                safe_parts.append(
                    {"kind": "at", "target_ref": None, "all_members": True}
                )
            else:
                safe_parts.append({"kind": "unknown", "source_ref": source})
        parent_event = (
            self.source.platform_event(scope, row["reply_to_id"])
            if row["reply_to_id"]
            else None
        )
        parent = (
            self.graph.get(scope, stable_id("message", scope, parent_event))
            if parent_event and parent_event != row["event_id"]
            else None
        )
        message = entity(
            "Message",
            scope,
            message_id,
            stamp,
            sources=[source],
            platform_message_id=row["platform_message_id"] or None,
            author_ref=ref(person),
            event_time=stamp,
            recorded_at=row["created_at"] * 1000,
            direction="outbound" if row["role"] == "assistant" else "inbound",
            origin="agent" if row["role"] == "assistant" else "member",
            parts=safe_parts or [{"kind": "unknown", "source_ref": source}],
            reply_to_ref=ref(parent) if parent else None,
            delivery_status=row["delivery_status"]
            or ("sent" if row["role"] == "assistant" else "received"),
        )
        nodes = [group, person, message]
        links = [
            edge("authored", person, message, stamp),
            edge("in_scope", message, group, stamp),
        ]
        links.extend(
            edge("contains_media", message, asset, stamp) for asset in media_nodes
        )
        if parent:
            links.append(edge("replies_to", message, parent, stamp))
        if person["type"] == "Person":
            membership_id = stable_id("membership", scope, person["id"])
            current = self.graph.get(scope, membership_id)
            membership = entity(
                "Membership",
                scope,
                membership_id,
                current["created_at"] if current else stamp,
                person_ref=ref(person),
                group_ref=ref(group),
                nickname=row["nickname"][:100],
                card=current["card"] if current else "",
                role=current["role"] if current else "unknown",
                status="active",
                observed_at=stamp,
            )
            membership["updated_at"] = stamp
            nodes.append(membership)
            links.extend(
                [
                    edge("represents", membership, person, stamp),
                    edge("member_of", membership, group, stamp),
                ]
            )
        self.graph.write(
            scope,
            nodes,
            links,
            reason="actual-conversation-projection",
        )
