import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from langchain_core.documents import Document

from evaluation.benchmark_retrieval import (
    find_gold_pages,
    load_cases,
    normalize_text,
    ranking_metrics,
    RetrievalCase,
)


class RetrievalBenchmarkTest(unittest.TestCase):
    def test_dataset_loads_unique_cases(self):
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "retrieval_cases.jsonl"
            dataset.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"id": "one", "query": "q1", "anchor": "a1"}
                        ),
                        json.dumps(
                            {"id": "two", "query": "q2", "anchor": "a2"}
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            cases = load_cases(dataset)

        self.assertEqual(len(cases), 2)
        self.assertEqual(len({case.id for case in cases}), 2)

    def test_normalization_allows_whitespace_and_width_differences(self):
        self.assertEqual(normalize_text("Ａgent  执行\n超时"), "agent执行超时")

    def test_gold_pages_are_located_from_answer_anchors(self):
        documents = [
            Document(page_content="第一部分：索引状态机"),
            Document(page_content="第二部分：同一会话要串行执行"),
        ]
        cases = [
            RetrievalCase(
                id="session",
                query="并发请求怎样隔离？",
                anchor="同一会话要串行执行",
            )
        ]

        self.assertEqual(find_gold_pages(documents, cases), {"session": [1]})

    def test_ranking_metrics_report_hit_rates_and_mrr(self):
        metrics = ranking_metrics(
            [
                ([2, 1, 0], {2}),
                ([3, 4, 5], {4}),
                ([6, 7, 8], {9}),
            ]
        )

        self.assertEqual(metrics["hit_at_1"], 0.3333)
        self.assertEqual(metrics["hit_at_3"], 0.6667)
        self.assertEqual(metrics["mrr_at_5"], 0.5)


if __name__ == "__main__":
    unittest.main()
