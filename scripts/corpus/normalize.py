"""QCE v6 single-file JSON -> versioned local conversation records."""

from datetime import datetime, timezone

from .privacy import IdentityMap


PLACEHOLDERS = {
    "image": "[图片]",
    "audio": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[转发消息]",
    "json": "[卡片]",
    "location": "[位置]",
    "system": "[系统消息]",
}
KNOWN_TYPES = {"text", "at", "reply", "face", "market_face", *PLACEHOLDERS}


def normalize(
    row: dict,
    group: str,
    source_hash: str,
    index: int,
    identities: IdentityMap,
    bot_ids: set[str],
) -> dict:
    raw_id = str(row.get("id") or "")
    if not raw_id or raw_id == "0":
        raise ValueError("missing_message_id")
    stamp = row.get("timestamp")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        raise ValueError("invalid_timestamp")
    # QCE declares milliseconds; accepting seconds is explicit compatibility, never local-time guessing.
    stamp = int(stamp if stamp >= 100_000_000_000 else stamp * 1000)
    try:
        dt = datetime.fromtimestamp(stamp / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ValueError("invalid_timestamp") from None
    sender = row.get("sender")
    if not isinstance(sender, dict):
        raise ValueError("invalid_sender")
    speaker = identities.person(
        sender.get("uin")
        if sender.get("uin") not in (None, "", "0")
        else sender.get("uid")
    )
    system = bool(row.get("system"))
    if speaker is None and not system:
        raise ValueError("missing_sender_identity")
    content = row.get("content")
    if not isinstance(content, dict) or not isinstance(
        content.get("elements", []), list
    ):
        raise ValueError("invalid_content")
    chunks, segments, flags, reply_ids, mentions = [], [], [], [], []
    own_text = []
    for element in content.get("elements", []):
        if not isinstance(element, dict):
            raise ValueError("invalid_element")
        kind = element.get("type", "unknown")
        data = element.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("invalid_element_data")
        segment = {"type": kind if kind in KNOWN_TYPES else "unknown"}
        if kind == "text":
            value = data.get("text", "")
            if not isinstance(value, str):
                raise ValueError("invalid_text")
            text, redactions = identities.text(value)
            flags.extend(redactions)
            segment["text"] = text
            chunks.append(text)
            own_text.append(text)
        elif kind == "at":
            person = (
                "all"
                if data.get("atType") == 1 or data.get("uid") == "all"
                else identities.person(data.get("uid") or data.get("uin"))
            )
            segment["user_id"] = person
            mentions.append(person)
            chunks.append("@" + (person or "unknown"))
        elif kind == "reply":
            ref = data.get("referencedMessageId")
            # Do not turn QCE's fallback messageId=0 or guessed preview into a true reference.
            if ref and str(ref) != "0":
                segment["message_id"] = identities.token("msg", group + ":" + str(ref))
                reply_ids.append(segment["message_id"])
            else:
                flags.append("unresolved_reply")
        elif kind in {"face", "market_face"}:
            face_id = str(data.get("id") or "")
            segment["face_id"] = (
                face_id if face_id.isdecimal() and len(face_id) <= 8 else None
            )
            chunks.append("[QQ表情]" if kind == "face" else "[商城表情]")
        else:
            chunks.append(PLACEHOLDERS.get(kind, "[未知消息元素]"))
            if kind not in KNOWN_TYPES:
                flags.append("unknown_element")
        segments.append(segment)
    if not segments:
        fallback = content.get("text", "")
        if not isinstance(fallback, str):
            raise ValueError("invalid_text")
        text, redactions = identities.text(fallback)
        flags.extend(redactions)
        flags.append("text_fallback")
        chunks.append(text)
        own_text.append(text)
    types = {e["type"] for e in segments}
    if types & {
        "image",
        "audio",
        "video",
        "file",
        "forward",
        "json",
        "location",
        "unknown",
    }:
        flags.append("media_dependent")
    if row.get("recalled"):
        flags.append("recalled")
        # Keep the event, not the withdrawn content, in derived datasets.
        chunks, own_text, segments, reply_ids, mentions = ["[已撤回]"], [], [], [], []
    return {
        "schema_version": 1,
        "id": identities.token("msg", group + ":" + raw_id),
        "group_id": identities.token("group", group),
        "speaker_id": speaker,
        "speaker_kind": "system"
        if system
        else ("bot" if speaker in bot_ids else "unknown"),
        "timestamp_ms": stamp,
        "time_utc": dt.isoformat(),
        "sequence": str(row.get("seq") or ""),
        "text": "".join(chunks),
        "own_text": "".join(own_text).strip(),
        "segments": segments,
        "reply_to": list(dict.fromkeys(reply_ids)),
        "mentions": list(dict.fromkeys(mentions)),
        "flags": sorted(set(flags)),
        "source": {"sha256": source_hash, "row": index},
    }
