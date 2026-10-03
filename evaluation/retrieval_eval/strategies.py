"""Four retrieval strategies used only by Retrieval Evaluation V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from langchain_core.documents import Document

from server.rag.retrieval import BM25Index, reciprocal_rank_fusion


@dataclass(frozen=True)
class RetrievalResult:
    documents: list[Document]
    stage_ms: dict[str, float]
    dense_candidates: list[Document] = field(default_factory=list)
    fused_candidates: list[Document] = field(default_factory=list)


class RetrievalStrategy(Protocol):
    name: str

    def retrieve(self, query: str) -> RetrievalResult: ...


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1_000


def synchronize_device(device: str) -> None:
    if not device.lower().startswith("cuda"):
        return
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA timing requested but CUDA is unavailable")
    torch.cuda.synchronize()


class DenseStrategy:
    name = "dense"

    def __init__(self, vectorstore: Any, *, top_k: int = 5):
        self.vectorstore = vectorstore
        self.top_k = top_k

    def retrieve(self, query: str) -> RetrievalResult:
        started = perf_counter()
        documents = self.vectorstore.similarity_search(query, k=self.top_k)
        dense_ms = _elapsed_ms(started)
        return RetrievalResult(
            documents=documents,
            dense_candidates=documents,
            stage_ms={"dense_ms": dense_ms, "total_ms": dense_ms},
        )


class BM25Strategy:
    name = "bm25"

    def __init__(self, index: BM25Index, *, top_k: int = 5):
        self.index = index
        self.top_k = top_k

    def retrieve(self, query: str) -> RetrievalResult:
        started = perf_counter()
        documents = self.index.search(query, self.top_k)
        bm25_ms = _elapsed_ms(started)
        return RetrievalResult(
            documents=documents,
            stage_ms={"bm25_ms": bm25_ms, "total_ms": bm25_ms},
        )


class _HybridBase:
    def __init__(
        self,
        vectorstore: Any,
        index: BM25Index,
        *,
        candidate_k: int = 20,
        top_k: int = 5,
    ):
        if top_k <= 0 or candidate_k < top_k:
            raise ValueError("candidate_k must be >= top_k > 0")
        self.vectorstore = vectorstore
        self.index = index
        self.candidate_k = candidate_k
        self.top_k = top_k

    def _candidates(
        self,
        query: str,
    ) -> tuple[list[Document], list[Document], dict[str, float]]:
        dense_started = perf_counter()
        dense = self.vectorstore.similarity_search(query, k=self.candidate_k)
        dense_ms = _elapsed_ms(dense_started)

        bm25_started = perf_counter()
        lexical = self.index.search(query, self.candidate_k)
        bm25_ms = _elapsed_ms(bm25_started)

        rrf_started = perf_counter()
        fused = reciprocal_rank_fusion([dense, lexical], self.candidate_k)
        rrf_ms = _elapsed_ms(rrf_started)
        return dense, fused, {
            "dense_ms": dense_ms,
            "bm25_ms": bm25_ms,
            "rrf_ms": rrf_ms,
        }


class HybridRRFStrategy(_HybridBase):
    name = "hybrid_rrf"

    def retrieve(self, query: str) -> RetrievalResult:
        total_started = perf_counter()
        dense, fused, stages = self._candidates(query)
        stages["total_ms"] = _elapsed_ms(total_started)
        return RetrievalResult(
            documents=fused[: self.top_k],
            dense_candidates=dense,
            fused_candidates=fused,
            stage_ms=stages,
        )


class HybridRerankStrategy(_HybridBase):
    name = "hybrid_reranker"

    def __init__(
        self,
        vectorstore: Any,
        index: BM25Index,
        reranker: Any,
        *,
        candidate_k: int = 20,
        top_k: int = 5,
        device: str = "cpu",
    ):
        super().__init__(
            vectorstore,
            index,
            candidate_k=candidate_k,
            top_k=top_k,
        )
        self.reranker = reranker
        self.device = device

    def retrieve(self, query: str) -> RetrievalResult:
        total_started = perf_counter()
        dense, fused, stages = self._candidates(query)
        synchronize_device(self.device)
        rerank_started = perf_counter()
        ranked = self.reranker.rerank(query, fused)
        synchronize_device(self.device)
        stages["reranker_ms"] = _elapsed_ms(rerank_started)
        stages["total_ms"] = _elapsed_ms(total_started)
        return RetrievalResult(
            documents=ranked[: self.top_k],
            dense_candidates=dense,
            fused_candidates=fused,
            stage_ms=stages,
        )
