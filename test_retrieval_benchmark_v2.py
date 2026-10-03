import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from langchain_core.documents import Document

from evaluation.benchmark_retrieval_v2 import (
    dataset_audit,
    load_cases,
    locate_gold_pages,
)


class RetrievalBenchmarkV2Test(unittest.TestCase):
    def test_dataset_load_and_anchor_audit_are_deterministic(self):
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "cases.jsonl"
            dataset.write_text(
                json.dumps({
                    "id": "q1",
                    "query": "Where is the answer?",
                    "anchor": "unique answer anchor",
                    "category": "semantic",
                }) + "\n",
                encoding="utf-8",
            )
            cases = load_cases(dataset)
            pages = [
                Document(page_content="irrelevant", metadata={"page": 0}),
                Document(
                    page_content="A unique answer\nanchor appears here.",
                    metadata={"page": 4},
                ),
            ]

            gold, failures = locate_gold_pages(pages, cases)
            audit = dataset_audit(dataset, cases, gold, failures)

            self.assertEqual(gold, {"q1": [4]})
            self.assertEqual(failures, [])
            self.assertEqual(audit["query_count"], 1)
            self.assertEqual(audit["gold_page_distribution"], {"5": 1})
            self.assertEqual(audit["category_distribution"], {"semantic": 1})

    def test_duplicate_ids_are_rejected(self):
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "cases.jsonl"
            row = json.dumps({"id": "same", "query": "q", "anchor": "a"})
            dataset.write_text(row + "\n" + row + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "IDs must be unique"):
                load_cases(dataset)

    def test_unlocated_anchor_is_reported(self):
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "cases.jsonl"
            dataset.write_text(
                json.dumps({"id": "q1", "query": "q", "anchor": "missing"}) + "\n",
                encoding="utf-8",
            )
            cases = load_cases(dataset)

            gold, failures = locate_gold_pages(
                [Document(page_content="other", metadata={"page": 0})],
                cases,
            )

            self.assertEqual(gold, {})
            self.assertEqual(failures[0]["id"], "q1")


if __name__ == "__main__":
    unittest.main()
