"""Route QQ platform events before they enter the conversation use case."""
from __future__ import annotations

import logging
from dataclasses import replace

from qqbot.agent.conversation.channels import ChannelPolicy


class QqEventRouter:
    def __init__(self, *, service, gateway, allowed_groups: frozenset[str],
                 private_enabled: bool, poke_controller, inbound_handlers, parse_event,
                 logger: logging.Logger | None = None):
        self.service = service
        self.gateway = gateway
        self.channels = ChannelPolicy(allowed_groups, private_enabled)
        self.poke_controller = poke_controller
        self.inbound_handlers = tuple(inbound_handlers)
        self.parse_event = parse_event
        self.log = logger if logger is not None else logging.getLogger(__name__)

    async def __call__(self, raw: dict) -> None:
        if await self.poke_controller.handle(raw):
            return
        for handler in self.inbound_handlers:
            try:
                if await handler(raw):
                    return
            except Exception:
                self.log.exception("Extension inbound handler failed")
        event = self.parse_event(raw)
        if event is None or not self.channels.accepts(event):
            return
        try:
            if event.reply_to_message_id:
                try:
                    quoted = await self.gateway.resolve_quote(
                        event.reply_to_message_id, group_id=event.group_id
                    )
                    event = replace(event, quoted_text=str(quoted.get("text") or ""),
                        quoted_image_urls=tuple(quoted.get("image_urls") or ()))
                except Exception:
                    self.log.info("Could not resolve quoted message %s", event.reply_to_message_id,
                                  exc_info=True)
            await self.service.handle_message(event)
        except Exception:
            self.log.exception("Failed to handle QQ event %s", event.event_id)
