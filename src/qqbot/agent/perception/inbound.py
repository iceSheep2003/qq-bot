"""Inbound media use case. Acquisition and byte storage are injected ports."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from qqbot.contracts.schema import ContractError
from qqbot.agent.conversation.parts import base_parts
from qqbot.contracts.ontology.objects import ref


class InboundMedia:
    def __init__(self, source, assets, ledger):
        self.source, self.assets, self.ledger = source, assets, ledger

    async def prepare(self, event):
        parts = base_parts(event)
        budget = 2
        for index, part in enumerate(event.inbound_parts):
            if part.kind not in {"image", "audio"} or budget <= 0:
                continue
            budget -= 1
            try:
                payload = await asyncio.wait_for(self.source.acquire(part), timeout=8)
                asset = self.assets.import_bytes(
                    event.scope,
                    payload,
                    source_ref=event.event_id,
                    expected_kind=part.kind,
                    now=event.timestamp * 1000,
                )
                parts[index] = {"kind": part.kind, "asset_ref": ref(asset)}
            except (ContractError, ConnectionError, TimeoutError, OSError, ValueError):
                # The real media stays unknown, never a guessed caption. Do not
                # persist exception strings containing signed platform URLs.
                continue
        self.ledger.attach_parts(event.event_id, event.scope, parts)
        return replace(
            event, ledger_parts=tuple(parts), image_urls=(), quoted_image_urls=()
        )
