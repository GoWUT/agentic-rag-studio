import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from server.agent.graph import build_agent
from server.agent.schemas import QueryAnalysis


class RecordingHarness:
    def __init__(self):
        self.received = []
        self.last_report = None

    def prepare(self, messages):
        current = list(messages)
        self.received.append(current)
        self.last_report = SimpleNamespace(as_dict=lambda: {})
        return SimpleNamespace(messages=current)


class StructuredDirect:
    def invoke(self, messages):
        return {
            "raw": AIMessage(content="{}"),
            "parsed": QueryAnalysis(
                standalone_query="hello",
                query_type="direct",
                needs_retrieval=False,
                selected_sources=[],
            ),
            "parsing_error": None,
        }


class FakeChatModel:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.invoked_with = None
        self.__class__.instances.append(self)

    def with_structured_output(self, schema, **kwargs):
        return StructuredDirect()

    def invoke(self, messages):
        self.invoked_with = list(messages)
        return AIMessage(content="answer")


class AgentContextIntegrationTest(unittest.TestCase):
    def test_final_generation_and_structured_calls_use_context_harness(self):
        harness = RecordingHarness()
        FakeChatModel.instances.clear()

        with patch("server.agent.graph.ChatOpenAI", FakeChatModel):
            agent = build_agent(
                "test-model",
                "test-key",
                [],
                context_harness=harness,
                max_output_tokens=321,
                request_timeout_seconds=45,
            )
            result = agent.invoke({"messages": [HumanMessage(content="hello")]})

        model = FakeChatModel.instances[0]
        self.assertGreaterEqual(len(harness.received), 2)
        self.assertTrue(any(isinstance(message, SystemMessage) for message in harness.received[-1]))
        self.assertEqual(model.invoked_with, harness.received[-1])
        self.assertEqual(model.kwargs["max_tokens"], 321)
        self.assertEqual(model.kwargs["timeout"], 45)
        self.assertEqual(model.kwargs["max_retries"], 0)
        self.assertEqual(result["messages"][-1].content, "answer")
        self.assertIsNotNone(harness.last_report)


if __name__ == "__main__":
    unittest.main()
