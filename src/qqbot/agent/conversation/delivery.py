"""Actual-delivery ledger and conversation projection, independent of turn execution."""
from __future__ import annotations

import logging
import time

from qqbot.contracts.models import MessageEvent
from qqbot.contracts.ports import ConversationRepository, ConversationIntelligencePort, PromptSessionRepository, ReplyDelivery


class ConversationDelivery:
    def __init__(self, conversations: ConversationRepository, reply_dispatcher: ReplyDelivery,
                 *, prompt_sessions: PromptSessionRepository | None = None,
                 conversation_intelligence: ConversationIntelligencePort | None = None,
                 logger: logging.Logger | None = None):
        self.conversations = conversations
        self.reply_dispatcher = reply_dispatcher
        self.prompt_sessions = prompt_sessions
        self.conversation_intelligence = conversation_intelligence
        self.log = logger if logger is not None else logging.getLogger(__name__)

    def record_transcript(self, event: MessageEvent, text: str) -> None:
        """Legacy whole replies are recorded only when no part receipts exist."""
        if not getattr(self.reply_dispatcher, "supports_part_receipts", False):
            self.conversations.add_message(
                f"reply:{event.event_id}", event.scope, "bot", "Bot", "assistant", text
            )

    def record_assistant(
        self, trigger: MessageEvent, text: str, *, event_id: str
    ) -> None:
        """Persist and index one actual outbound turn through one boundary."""
        has_delivery = getattr(self.conversations, "has_delivery", None)
        if not (callable(has_delivery) and has_delivery(f"reply:{trigger.event_id}")):
            inserted = self.conversations.add_message(
                event_id, trigger.scope, "bot", "Bot", "assistant", text
            )
            if inserted is False:
                return
        if self.prompt_sessions is not None and text.strip():
            try:
                self.prompt_sessions.append(
                    trigger.scope, [{"role": "assistant", "content": text.strip()}]
                )
            except Exception:
                self.log.exception("Could not sync local assistant turn event=%s", event_id)
        self.observe_assistant(trigger, text, event_id)

    def observe_assistant(
        self, trigger: MessageEvent, text: str, event_id: str
    ) -> None:
        if self.conversation_intelligence is None or not trigger.group_id:
            return
        reply_to = trigger.platform_message_id
        if trigger.origin == "operator":
            latest = self.conversation_intelligence.latest_frame(trigger.scope)
            reply_to = latest.focus.platform_message_id if latest is not None else ""
        self.conversation_intelligence.observe(
            MessageEvent(
                event_id=event_id,
                scope=trigger.scope,
                group_id=trigger.group_id,
                user_id="bot",
                nickname="Kinna",
                text=text,
                image_urls=(),
                at_bot=False,
                at_users=(),
                timestamp=int(time.time()),
                platform_message_id=event_id,
                reply_to_message_id=reply_to,
                reply_to_user_id=trigger.user_id
                if trigger.origin != "operator"
                else "",
                origin="assistant",
            )
        )

    async def deliver(self, plan, *, group_id, user_id, allowed_at, event=None):
        kwargs = {"group_id": group_id, "user_id": user_id, "allowed_at": allowed_at}
        if event is not None and getattr(
            self.reply_dispatcher, "supports_part_receipts", False
        ):
            begin_part = getattr(self.conversations, "begin_part", None)
            if callable(begin_part):

                async def intent(index, kind, text):
                    return begin_part(
                        f"reply:{event.event_id}", index, event.scope, kind, text
                    )

                kwargs["on_intent"] = intent

            async def record(receipt):
                settle = getattr(self.conversations, "settle_part", None)
                if callable(settle):
                    settle(f"reply:{event.event_id}", receipt)
                if receipt.status != "sent":
                    self.log.warning(
                        "Uncertain delivery event=%s part=%s",
                        event.event_id,
                        receipt.index,
                    )
                    return
                event_id = f"part:reply:{event.event_id}:{receipt.index}"
                metadata = {
                    "origin": "agent",
                    "event_time": int(time.time() * 1000),
                    "platform_message_id": receipt.platform_message_id,
                    "reply_to_id": plan.quote_message_id,
                    "parent_id": f"reply:{event.event_id}",
                    "delivery_status": "sent",
                    "parts": [{"kind": "text", "text": receipt.transcript}],
                }
                # Concrete store supports receipt metadata; small test adapters
                # keep the original narrow repository interface.
                if callable(getattr(self.conversations, "actual_recent", None)):
                    self.conversations.add_message(
                        event_id,
                        event.scope,
                        "bot",
                        "Bot",
                        "assistant",
                        receipt.transcript,
                        metadata=metadata,
                    )
                else:
                    self.conversations.add_message(
                        event_id,
                        event.scope,
                        "bot",
                        "Bot",
                        "assistant",
                        receipt.transcript,
                    )
                self.observe_assistant(event, receipt.transcript, event_id)

            kwargs["on_receipt"] = record
        return await self.reply_dispatcher.dispatch(plan, **kwargs)

