import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from server.agent.graph import build_agent


class RecordingHarness:
    def __init__(self):
        self.received = None
        self.prepared = [
            SystemMessage(content="bounded system"),
            HumanMessage(content="bounded question"),
        ]

    def prepare(self, messages):
        self.received = list(messages)
        return SimpleNamespace(messages=self.prepared)


class FakeChatModel:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.invoked_with = None
        self.__class__.instances.append(self)

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.invoked_with = list(messages)
        return AIMessage(content="answer")


class AgentContextIntegrationTest(unittest.TestCase):
    def test_every_llm_call_passes_through_context_harness(self):
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
            result = agent.invoke(
                {"messages": [HumanMessage(content="original question")]}
            )

        model = FakeChatModel.instances[0]
        self.assertIsNotNone(harness.received)
        self.assertTrue(
            any(isinstance(message, SystemMessage) for message in harness.received)
        )
        self.assertEqual(model.invoked_with, harness.prepared)
        self.assertEqual(model.kwargs["max_tokens"], 321)
        self.assertEqual(model.kwargs["timeout"], 45)
        self.assertEqual(model.kwargs["max_retries"], 0)
        self.assertEqual(result["messages"][-1].content, "answer")


if __name__ == "__main__":
    unittest.main()
