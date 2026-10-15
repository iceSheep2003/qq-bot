"""Trusted scene classification and canonical conversation admission."""

from dataclasses import dataclass


def channel_for(event):
    channel = "group" if event.group_id else "private"
    target = event.group_id if event.group_id else event.user_id
    if not target or event.scope != f"{channel}:{target}":
        raise ValueError("Conversation identity and scope disagree")
    return channel


@dataclass(frozen=True)
class ChannelPolicy:
    allowed_groups: frozenset[str]
    private_enabled: bool = False

    def accepts(self, event):
        try:
            channel = channel_for(event)
        except ValueError:
            return False
        if channel == "group":
            return event.group_id in self.allowed_groups
        return self.private_enabled
