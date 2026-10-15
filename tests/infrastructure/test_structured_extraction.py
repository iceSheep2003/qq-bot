import unittest

from qunbot.infrastructure.adapters.structured import InstructorOutput
from qunbot.contracts.schema import ContractError
from qunbot.agent.memory.knowledge.extraction_models import ExtractionBatch


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages, **kwargs):
        self.requests.append(messages)
        return {
            "choices": [
                {"message": {"role": "assistant", "content": self.responses.pop(0)}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


class InstructorTests(unittest.IsolatedAsyncioTestCase):
    async def test_types_and_original_usage(self):
        model = Model(['{"reports":[],"slang":[]}'])
        data, usage = await InstructorOutput(model).propose(
            [{"role": "user", "content": "synthetic"}], ExtractionBatch
        )
        self.assertEqual(data, {"reports": [], "slang": []})
        self.assertNotIn("prompt_tokens_details", usage)
        self.assertIn("JSON", str(model.requests))

    async def test_framework_repairs_invalid_output_and_counts_calls(self):
        model = Model(
            ['{"unexpected":"private-test-value"}', '{"reports":[],"slang":[]}']
        )
        data, usage = await InstructorOutput(model).propose(
            [{"role": "user", "content": "synthetic"}], ExtractionBatch
        )
        self.assertEqual(data["reports"], [])
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(usage["total_tokens"], 30)

    async def test_failure_is_bounded_and_does_not_expose_response(self):
        model = Model(["private-test-value", "private-test-value"])
        with self.assertRaises(ContractError) as context:
            await InstructorOutput(model).propose(
                [{"role": "user", "content": "synthetic"}], ExtractionBatch
            )
        self.assertNotIn("private-test-value", str(context.exception))
        self.assertEqual(len(model.requests), 2)
