import unittest

from langchain_core.documents import Document

from evaluation.retrieval_eval.strategies import (
    BM25Strategy,
    DenseStrategy,
    HybridRerankStrategy,
    HybridRRFStrategy,
)
from server.rag.retrieval import BM25Index


def document(text, page):
    return Document(page_content=text, metadata={"page": page})


class FakeVectorStore:
    def __init__(self, ranking):
        self.ranking = ranking
        self.calls = []

    def similarity_search(self, query, k):
        self.calls.append((query, k))
        return self.ranking[:k]


class ReverseReranker:
    def rerank(self, query, documents):
        return list(reversed(documents))


class FailingReranker:
    def rerank(self, query, documents):
        raise RuntimeError("reranker failed")


class RetrievalStrategiesV2Test(unittest.TestCase):
    def setUp(self):
        self.documents = [
            document("dense semantic content", 1),
            document("rare lexical token", 2),
            document("other material", 3),
        ]
        self.vectorstore = FakeVectorStore(self.documents)
        self.bm25 = BM25Index(self.documents)

    def test_dense_strategy_uses_exact_top_k(self):
        result = DenseStrategy(self.vectorstore, top_k=2).retrieve("query")

        self.assertEqual(self.vectorstore.calls, [("query", 2)])
        self.assertEqual([item.metadata["page"] for item in result.documents], [1, 2])

    def test_bm25_strategy_reuses_existing_index(self):
        strategy = BM25Strategy(self.bm25, top_k=2)

        result = strategy.retrieve("rare lexical token")

        self.assertEqual(result.documents[0].metadata["page"], 2)
        self.assertIs(strategy.index, self.bm25)

    def test_hybrid_rrf_uses_candidate_k_then_returns_top_k(self):
        result = HybridRRFStrategy(
            self.vectorstore,
            self.bm25,
            candidate_k=3,
            top_k=2,
        ).retrieve("rare lexical token")

        self.assertEqual(self.vectorstore.calls[-1], ("rare lexical token", 3))
        self.assertEqual(len(result.fused_candidates), 3)
        self.assertEqual(len(result.documents), 2)

    def test_hybrid_reranker_changes_order(self):
        strategy = HybridRerankStrategy(
            self.vectorstore,
            self.bm25,
            ReverseReranker(),
            candidate_k=3,
            top_k=2,
        )

        result = strategy.retrieve("rare lexical token")

        self.assertEqual(result.documents, list(reversed(result.fused_candidates))[:2])
        self.assertIn("reranker_ms", result.stage_ms)

    def test_reranker_failure_is_not_silently_fallbacked(self):
        strategy = HybridRerankStrategy(
            self.vectorstore,
            self.bm25,
            FailingReranker(),
            candidate_k=3,
            top_k=2,
        )

        with self.assertRaisesRegex(RuntimeError, "reranker failed"):
            strategy.retrieve("query")


if __name__ == "__main__":
    unittest.main()
