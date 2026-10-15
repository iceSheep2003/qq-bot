"""Instructor 1.17 structured output adapter over the existing model port.

Instructor owns schema prompting, typed decoding and bounded repair retries.
The existing model adapter still owns credentials, HTTP retry, cache metrics,
circuit breakers and budgets. The framework has no graph-writing authority.
"""

from __future__ import annotations

import time
import logging
from contextvars import ContextVar

import instructor
from openai.types.chat import ChatCompletion
from tenacity import AsyncRetrying, stop_after_attempt, wait_none

from qqbot.contracts.schema import ContractError


class _PrivateFrameworkLogs(logging.Filter):
    def filter(self, record):
        # Instructor logs validation exceptions containing model output at
        # ERROR level. Keep the failure event, not the embedded private value.
        record.msg = "Structured framework diagnostic (content suppressed)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


for _logger in ("instructor", "instructor.v2", "instructor.v2.retry"):
    logging.getLogger(_logger).addFilter(_PrivateFrameworkLogs())


class InstructorOutput:
    def __init__(self, model, *, max_attempts=2):
        if not 1 <= max_attempts <= 3:
            raise ValueError("Structured retries must be bounded")
        self.model = model
        self.max_attempts = max_attempts
        self._usage = ContextVar("structured_usage", default=None)
        # create= is the official custom-provider entry point. No second HTTP
        # client or second secret store is introduced by the framework.
        self.client = instructor.AsyncInstructor(
            client=None,
            create=instructor.patch(create=self._create, mode=instructor.Mode.JSON),
            mode=instructor.Mode.JSON,
        )

    async def _create(self, **kwargs):
        response = await self.model.complete(
            kwargs["messages"], temperature=kwargs.get("temperature", 0.0)
        )
        usage = self._usage.get()
        if usage is not None:
            usage.append(response.get("usage") or {})
        # Model port supports multiple compatible providers; normalize only the
        # OpenAI response envelope required by Instructor, not message content.
        normalized = {
            "id": "structured-output",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "configured-provider",
            **response,
        }
        normalized["choices"] = [
            {"index": i, "finish_reason": "stop", **choice}
            for i, choice in enumerate(response["choices"])
        ]
        return ChatCompletion.model_validate(normalized)

    async def propose(self, messages, response_model):
        calls = []
        token = self._usage.set(calls)
        try:
            proposal, response = await self.client.create_with_completion(
                messages=messages,
                response_model=response_model,
                temperature=0.0,
                max_retries=AsyncRetrying(
                    stop=stop_after_attempt(self.max_attempts), wait=wait_none()
                ),
                strict=True,
            )
        except Exception:
            # Framework validation exceptions may embed the raw conversation
            # or model answer. Never let those reach application logs.
            raise ContractError(
                "Structured extraction failed after bounded framework validation"
            ) from None
        finally:
            self._usage.reset(token)
        # Use original provider telemetry. OpenAI's response DTO fills absent
        # cached-token details with zero; that is not a cache observation.
        usage = dict(calls[-1]) if calls else {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if calls and all(key in c for c in calls):
                usage[key] = sum(c[key] for c in calls)
        return proposal.model_dump(), usage
