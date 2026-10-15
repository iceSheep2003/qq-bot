from __future__ import annotations

from qqbot.contracts.models import InboundPart, MessageEvent


def parse_message(data: dict) -> MessageEvent | None:
    if data.get("post_type") != "message" or data.get("message_type") not in {
        "group",
        "private",
    }:
        return None
    if data.get("sub_type") == "self" or str(data.get("user_id")) == str(
        data.get("self_id")
    ):
        return None
    group = str(data["group_id"]) if data.get("message_type") == "group" else None
    user = str(data.get("user_id", ""))
    if not user:
        return None
    parts = data.get("message", [])
    if not isinstance(parts, list):
        parts = [{"type": "text", "data": {"text": str(data.get("raw_message", ""))}}]
    parts = [
        p
        for p in parts[:100]
        if isinstance(p, dict) and isinstance(p.get("data", {}), dict)
    ]
    structured = []
    for part in parts:
        kind, payload = part.get("type"), part.get("data", {})
        if kind == "text":
            structured.append(
                InboundPart("text", text=str(payload.get("text", ""))[:12000])
            )
        elif kind in {"image", "record"}:
            structured.append(
                InboundPart(
                    "image" if kind == "image" else "audio",
                    file_ref=str(payload.get("file", ""))[:2000],
                    url=str(payload.get("url", ""))[:4000],
                )
            )
        elif kind == "face":
            structured.append(
                InboundPart("face", face_id=str(payload.get("id", ""))[:160])
            )
        elif kind in {"at", "reply"}:
            structured.append(
                InboundPart(
                    "at" if kind == "at" else "quote",
                    target_id=str(payload.get("qq" if kind == "at" else "id", ""))[
                        :160
                    ],
                )
            )
        else:
            structured.append(InboundPart("unknown"))
    text = "".join(
        str(p.get("data", {}).get("text", "")) for p in parts if p.get("type") == "text"
    ).strip()
    images = tuple(
        str(p.get("data", {}).get("url", ""))
        for p in parts
        if p.get("type") == "image" and p.get("data", {}).get("url")
    )
    at_users = tuple(
        str(p.get("data", {}).get("qq", "")) for p in parts if p.get("type") == "at"
    )
    reply_parts = [p for p in parts if p.get("type") == "reply"]
    reply_data = reply_parts[0].get("data", {}) if reply_parts else {}
    sender = data.get("sender") or {}
    card = str(sender.get("card") or "").strip()
    nickname = str(sender.get("card") or sender.get("nickname") or user)
    return MessageEvent(
        event_id=f"{data.get('self_id')}:{data.get('message_id')}",
        scope=f"group:{group}" if group else f"private:{user}",
        group_id=group,
        user_id=user,
        nickname=nickname,
        text=text,
        image_urls=images,
        at_bot=str(data.get("self_id")) in at_users,
        at_users=at_users,
        timestamp=int(data.get("time") or 0),
        card=card,
        platform_message_id=str(data.get("message_id") or ""),
        reply_to_message_id=str(reply_data.get("id") or ""),
        reply_to_user_id=str(reply_data.get("user_id") or ""),
        inbound_parts=tuple(structured),
    )
