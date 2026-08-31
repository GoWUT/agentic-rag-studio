import unittest

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from server.agent.context_harness import ContextHarness


class ContextHarnessTest(unittest.TestCase):
    def make_harness(self, *, input_budget_tokens=220):
        return ContextHarness(
            context_window_tokens=400,
            input_budget_tokens=input_budget_tokens,
            max_output_tokens=100,
            safety_tokens=50,
            summary_tokens=60,
            recent_turns=2,
            chars_per_token=2.0,
        )

    def test_keeps_messages_unchanged_when_they_fit(self):
        harness = self.make_harness()
        messages = [
            SystemMessage(content="Use the PDF when relevant."),
            HumanMessage(content="What is the paper about?"),
            AIMessage(content="It presents a new RAG method."),
        ]

        prepared = harness.prepare(messages)

        self.assertEqual(prepared.messages, messages)
        self.assertEqual(prepared.report.strategy, "full")
        self.assertEqual(prepared.report.compacted_messages, 0)
        self.assertLessEqual(
            prepared.report.estimated_tokens_after,
            prepared.report.input_budget_tokens,
        )

    def test_compacts_old_turns_and_keeps_the_latest_question(self):
        harness = self.make_harness(input_budget_tokens=180)
        messages = [SystemMessage(content="You are a PDF assistant.")]
        for index in range(8):
            messages.extend(
                [
                    HumanMessage(
                        content=f"old question {index} " + "Q" * 80
                    ),
                    AIMessage(
                        content=f"old answer {index} " + "A" * 100
                    ),
                ]
            )
        messages.append(HumanMessage(content="LATEST QUESTION"))

        prepared = harness.prepare(messages)

        self.assertEqual(prepared.messages[0], messages[0])
        self.assertTrue(
            any(
                isinstance(message, HumanMessage)
                and "LATEST QUESTION" in str(message.content)
                for message in prepared.messages
            )
        )
        self.assertTrue(
            any(
                isinstance(message, SystemMessage)
                and "Context Harness memory" in str(message.content)
                for message in prepared.messages
            )
        )
        self.assertEqual(prepared.report.strategy, "compacted")
        self.assertGreater(prepared.report.compacted_messages, 0)
        self.assertLessEqual(
            prepared.report.estimated_tokens_after,
            prepared.report.input_budget_tokens,
        )

    def test_truncates_large_tool_output_without_breaking_tool_pair(self):
        harness = self.make_harness(input_budget_tokens=170)
        messages = [
            SystemMessage(content="Answer from the PDF."),
            HumanMessage(content="Summarize the experiment."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_pdf",
                        "args": {"query": "experiment"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="result " * 1000,
                tool_call_id="call-1",
                name="search_pdf",
            ),
        ]

        prepared = harness.prepare(messages)

        tool_call_index = next(
            index
            for index, message in enumerate(prepared.messages)
            if isinstance(message, AIMessage) and message.tool_calls
        )
        tool_result_index = next(
            index
            for index, message in enumerate(prepared.messages)
            if isinstance(message, ToolMessage)
        )
        tool_result = prepared.messages[tool_result_index]

        self.assertLess(tool_call_index, tool_result_index)
        self.assertEqual(tool_result.tool_call_id, "call-1")
        self.assertIn("truncated by Context Harness", str(tool_result.content))
        self.assertGreater(prepared.report.truncated_messages, 0)
        self.assertLessEqual(
            prepared.report.estimated_tokens_after,
            prepared.report.input_budget_tokens,
        )


if __name__ == "__main__":
    unittest.main()
