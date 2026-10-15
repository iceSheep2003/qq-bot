"""Pure mapping of observed platform parts to unresolved ledger parts."""

from __future__ import annotations


def base_parts(event):
    source = {"kind": "platform", "ref": event.event_id}
    if not event.inbound_parts:
        return (
            [{"kind": "text", "text": event.text}]
            if event.text
            else [{"kind": "unknown", "source_ref": source}]
        )
    parts = []
    for part in event.inbound_parts:
        if part.kind == "text":
            parts.append({"kind": "text", "text": part.text})
        elif part.kind == "face" and part.face_id:
            parts.append({"kind": "face", "face_id": part.face_id})
        elif part.kind == "at" and part.target_id == "all":
            parts.append({"kind": "at", "target_ref": None, "all_members": True})
        else:
            parts.append({"kind": "unknown", "source_ref": source})
    return parts
