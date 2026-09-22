from __future__ import annotations

from ..domain import MessageEvent


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
    sender = data.get("sender") or {}
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
    )
