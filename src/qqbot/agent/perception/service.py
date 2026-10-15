"""Host scoped media lookup + optional providers, with evidence-preserving cache."""

from __future__ import annotations
import asyncio

from qqbot.contracts.schema import fingerprint


class PerceptionService:
    def __init__(self, assets, *, vision=None, asr=None):
        self.assets, self.vision, self.asr = assets, vision, asr

    async def analyze(self, scope, identity, question):
        if self.vision is None:
            raise NotImplementedError("Vision not configured")
        asset, payload = self.assets.resolve(scope, identity, kind="image")
        key = fingerprint(["vision", asset["digest"], self.vision.version, question])
        result = self.assets.cached(scope, key)
        if result:
            return result
        analysis = await asyncio.wait_for(
            self.vision.analyze(payload, asset["mime"], question), timeout=30
        )
        result = {
            "asset_id": identity,
            "analysis": analysis[:6000],
            "uncertainty": "high",
            "source_refs": [asset["source_ref"]],
        }
        self.assets.cache(scope, key, result)
        return result

    async def transcribe(self, scope, identity):
        if self.asr is None:
            raise NotImplementedError("ASR not configured")
        asset, payload = self.assets.resolve(scope, identity, kind="audio")
        key = fingerprint(["asr", asset["digest"], self.asr.version])
        result = self.assets.cached(scope, key)
        if result:
            return result
        text = await asyncio.wait_for(
            self.asr.transcribe(payload, asset["mime"]), timeout=30
        )
        result = {
            "asset_id": identity,
            "transcript": text[:6000],
            "confidence": None,
            "source_refs": [asset["source_ref"]],
        }
        self.assets.cache(scope, key, result)
        return result
