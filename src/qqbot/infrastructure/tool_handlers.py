"""Bind fixed tool ports to built-in application services, never to chat commands."""

from __future__ import annotations
import json

from qqbot.actions.tools.kernel import KernelDenied
from qqbot.contracts.ontology.objects import stable_id
from qqbot.agent.conversation.channels import channel_for
from qqbot.agent.skills.audience import scoped_skills


class BuiltinHandlers:
    def __init__(
        self,
        conversations,
        knowledge,
        skills,
        *,
        legacy_tools=None,
        features=None,
        perception=None,
    ):
        self.conversations, self.knowledge, self.skills = conversations, knowledge, skills
        self.legacy_tools, self.features = legacy_tools, features
        self.runtime = None
        self.perception = perception

    def bind(self, runtime):
        self.runtime = runtime

    def handlers(self):
        handlers = {
            "conversation_read": self.conversation_read,
            "knowledge_search": self.knowledge_search,
            "knowledge_evidence": self.knowledge_evidence,
            "group_query": self.group_query,
            "media_catalog": self.media_catalog,
            "skill_load": self.skill_load,
            "interaction_propose": self.interaction_propose,
            "group_action_propose": self.group_action_propose,
        }
        if self.perception and self.perception.vision:
            handlers["vision_analyze"] = lambda a, e: self.perception.analyze(
                e.scope, a["asset_id"], a["question"]
            )
        if self.perception and self.perception.asr:
            handlers["audio_transcribe"] = lambda a, e: self.perception.transcribe(
                e.scope, a["asset_id"]
            )
        return handlers

    def conversation_read(self, args, event):
        rows = self.conversations.actual_recent(event.scope, 100)
        if args.get("message_ids"):
            allowed = set(args["message_ids"])
            rows = [
                r
                for r in rows
                if str(r["event_id"]) in allowed
                or str(r.get("platform_message_id") or "") in allowed
            ]
        messages = []
        for row in rows[-args["limit"] :]:
            parts = json.loads(row.get("parts_json") or "[]")
            asset_ids = [
                part["asset_ref"]["id"]
                for part in parts
                if part.get("kind") in {"image", "audio", "video", "file"}
                and isinstance(part.get("asset_ref"), dict)
                and part["asset_ref"].get("scope") == event.scope
            ][:20]
            messages.append(
                {
                    "id": str(row["event_id"]),
                    "author_id": str(row["user_id"]),
                    "text": row["content"][:12000],
                    "event_time": row.get("event_time") or row["created_at"] * 1000,
                    "direction": "outbound"
                    if row["role"] == "assistant"
                    else "inbound",
                    "delivery_status": row.get("delivery_status")
                    or ("sent" if row["role"] == "assistant" else "received"),
                    "asset_ids": asset_ids,
                    "reply_to_id": row.get("reply_to_id") or None,
                }
            )
        # Bound by bytes as well as count; keep the newest intact rows.
        while len(json.dumps(messages, ensure_ascii=False).encode()) > 56000:
            messages.pop(0)
        return {"messages": messages}

    def knowledge_search(self, args, event):
        subject = args.get("subject_id")
        if subject and self.knowledge.graph.get(event.scope, subject) is None:
            subject = stable_id("person", event.scope, subject)
        return {
            "hits": self.knowledge.search(
                event.scope,
                args["query"],
                args["limit"],
                subject_id=subject,
                as_of=args.get("as_of"),
                category=args.get("category"),
                include_candidates=True,
            )
        }

    def knowledge_evidence(self, args, event):
        return {"evidence": self.knowledge.evidence(event.scope, args["evidence_ids"])}

    def skill_load(self, args, event):
        skills = scoped_skills(self.skills, channel_for(event))
        loader = getattr(skills, "load", None)
        if callable(loader):
            return loader(args["skill_id"])
        skill = skills.by_name(args["skill_id"])
        if not skill:
            raise KernelDenied("Skill not owner-enabled")
        return {
            "skill_id": skill.name,
            "version": "legacy-instructions-v1",
            "instructions": skill.body[:12000],
            "required_capabilities": [],
        }

    def media_catalog(self, args, event):
        category = args["category"]
        items = []
        caps = self.features.reply_capabilities() if self.features else None
        if category == "meme" and caps:
            for tag in sorted(caps.meme_tags):
                if not args.get("tag") or args["tag"] in tag:
                    items.append(
                        {
                            "id": tag,
                            "label": tag,
                            "summary": "当前可用表情标签",
                            "source_refs": [],
                        }
                    )
        elif category == "voice" and caps and caps.voice:
            items = [
                {
                    "id": "voice:short",
                    "label": "短句语音",
                    "summary": "不超过15字、约20%的可选语音倾向，由回复planner决定",
                    "source_refs": [],
                }
            ]
        elif category == "qq_face":
            from qqbot.actions.replies.qq_faces import DEFAULT_QQ_FACES

            items = [
                {
                    "id": face_id,
                    "label": name,
                    "summary": "QQ 原生表情",
                    "source_refs": [],
                }
                for name, (face_id, _) in DEFAULT_QQ_FACES.items()
            ]
        elif category == "native":
            items = [
                {
                    "id": kind,
                    "label": kind,
                    "summary": "由内核配额与平台能力决定",
                    "source_refs": [],
                }
                for kind in ("poke", "reaction", "repeat_plus_one", "dice", "rps")
            ]
        return {"available": bool(items), "items": items[: args["limit"]]}

    async def group_query(self, args, event):
        if not self.runtime or not event.group_id:
            raise NotImplementedError()
        if args["category"] == "files":
            if not self.legacy_tools:
                raise NotImplementedError()
            rows = json.loads(
                await self.legacy_tools.acall("list_group_files", {}, event)
            )
            items = [
                {
                    "id": str(r["id"]),
                    "label": r["name"],
                    "summary": str(r.get("folder") or "群文件"),
                    "source_refs": [],
                }
                for r in rows
            ]
        elif args["category"] == "members":
            rows = await self.runtime.sender.call(
                "get_group_member_list", {"group_id": int(event.group_id)}
            )
            items = [
                {
                    "id": str(r["user_id"]),
                    "label": str(r.get("card") or r.get("nickname") or r["user_id"])[
                        :200
                    ],
                    "summary": str(r.get("role") or "unknown"),
                    "source_refs": [],
                }
                for r in rows[:1000]
            ]
        else:
            raise NotImplementedError()
        query = args.get("query", "")
        return {
            "category": args["category"],
            "items": [i for i in items if not query or query in i["label"]][
                : args["limit"]
            ],
        }

    async def interaction_propose(self, args, event):
        if not self.runtime or not event.group_id:
            raise NotImplementedError()
        kind = args["kind"]
        sender = self.runtime.sender
        if kind == "repeat_plus_one":
            # Reuse the existing persistent quota/content ledger, never a second
            # repeat policy. Ordinary model echoes cannot enter this path.
            if event.text.strip() != "+1":
                raise KernelDenied("Repeat requires an explicit +1")
            allowed = {
                r.get("platform_message_id") or r["event_id"]
                for r in self.conversations.actual_recent(event.scope, 8)
            }
            if args["message_id"] not in allowed:
                raise KernelDenied("Unknown repeat source")
            if not await self.runtime.maybe_light_interaction(event):
                raise KernelDenied("Repeat policy declined")
        elif kind == "reaction":
            permitted = {
                event.platform_message_id,
                event.reply_to_message_id,
                *event.recent_message_ids,
            } - {""}
            if args["message_id"] not in permitted:
                raise KernelDenied("Unknown reaction target")
            await sender.react_to_message(args["message_id"], args["emoji_id"])
            self.runtime.record_assistant_turn(
                event,
                f"[对消息{args['message_id']}贴表情:{args['emoji_id']}]",
                event_id=f"reaction:{event.event_id}:{args['emoji_id']}",
            )
        elif kind == "poke":
            if args["target_id"] not in {event.user_id, *event.at_users}:
                raise KernelDenied("Unknown poke target")
            await sender.poke_group(event.group_id, args["target_id"])
            self.runtime.record_assistant_turn(
                event,
                f"[戳一戳:{args['target_id']}]",
                event_id=f"poke:{event.event_id}:{args['target_id']}",
            )
        else:
            await sender.send_native(event.group_id, kind)
            self.runtime.record_assistant_turn(
                event, f"[{kind}]", event_id=f"native:{event.event_id}:{kind}"
            )
        return {
            "request_id": stable_id("interaction", event.event_id, args),
            "kind": kind,
            "state": "succeeded",
        }

    async def group_action_propose(self, args, event):
        if not self.legacy_tools:
            raise NotImplementedError()
        kind = args["kind"]
        if kind != "title" and event.origin != "operator":
            raise KernelDenied("Moderation is owner-code only")
        mapping = {
            "title": (
                "propose_group_title",
                {
                    "user_id": args.get("target_id"),
                    "title": args.get("title"),
                    "reason": args.get("reason"),
                },
            ),
            "mute": (
                "mute_group_member",
                {
                    "user_id": args.get("target_id"),
                    "duration": args.get("duration_seconds"),
                },
            ),
            "unmute": (
                "mute_group_member",
                {"user_id": args.get("target_id"), "duration": 0},
            ),
            "essence": (
                "set_group_essence",
                {"message_id": args.get("message_id"), "enable": args.get("enable")},
            ),
        }
        if kind not in mapping:
            raise NotImplementedError()
        name, parameters = mapping[kind]
        try:
            result = await self.legacy_tools.acall(name, parameters, event)
        except ValueError:
            raise KernelDenied("Built-in operation policy declined") from None
        if result.startswith("tool ") or result == "unknown tool":
            raise KernelDenied("Built-in action refused")
        return {
            "request_id": stable_id("operation", event.event_id, args),
            "kind": kind,
            "state": "awaiting_consent" if kind == "title" else "succeeded",
        }
