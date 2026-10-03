"""Bounded per-document candidate retrieval followed by one global rerank."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
import hashlib
import logging
import re
import time
from threading import BoundedSemaphore, RLock
from typing import Any, Callable, Mapping

from langchain_core.documents import Document

from server.rag.retrieval import CrossEncoderReranker, build_retriever
from server.workspaces import DocumentRecord, WorkspaceStore

LOGGER = logging.getLogger(__name__)


def comparison_targets(query: str, records: list[DocumentRecord]) -> set[str]:
    """Select explicitly named papers, or all papers for an unqualified cross-document task."""
    if not re.search(r"\bcompar(?:e|es|ing|ison|isons)\b|across\s+documents|each\s+paper|all\s+papers|分别|对比|比较|各论文", query, re.I):
        return set()
    if re.search(r'all\s+(?:\w+\s+)?papers|each\s+paper|across\s+documents|所有论文|各论文', query, re.I):
        return {record.id for record in records}
    named = set()
    for record in records:
        aliases = {Path(record.filename).stem, Path(record.display_name).stem}
        if any(alias and re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", query, re.I) for alias in aliases):
            named.add(record.id)
    return named if len(named) >= 2 else {record.id for record in records}


def select_coverage_hits(ranked: list[Document], targets: set[str], k: int) -> list[Document]:
    """Keep each target's highest-ranked available hit, then fill by global order."""
    if not targets:
        return ranked[:k]
    required: set[int] = set()
    covered: set[str] = set()
    for index, hit in enumerate(ranked):
        identity = hit.metadata.get("document_id")
        if identity in targets and identity not in covered:
            covered.add(identity)
            required.add(index)
    effective_k = max(k, len(required))
    selected = set(required)
    for index in range(len(ranked)):
        if len(selected) >= effective_k:
            break
        selected.add(index)
    return [hit for index, hit in enumerate(ranked) if index in selected]


class WorkspaceRetriever:
    """Read the registry every query so document additions/deletions take effect immediately."""

    def __init__(self, store: WorkspaceStore, workspace_id: str, config: Mapping[str, Any],
                 load_index: Callable[[DocumentRecord], Any], reranker: Any = None):
        self.store, self.workspace_id, self.config = store, workspace_id, config
        self.load_index = load_index
        self._cache: dict[str, Any] = {}
        self._lock = RLock()
        workers = config.get("WORKSPACE_RETRIEVAL_CONCURRENCY", 4)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="workspace-retrieval")
        self._slots = BoundedSemaphore(workers)
        self.reranker = reranker
        if reranker is None and config.get("RERANKER_ENABLED", True):
            self.reranker = CrossEncoderReranker(config["RERANKER_MODEL"], config.get("RERANKER_DEVICE", "cpu"), config.get("RERANKER_BATCH_SIZE", 8), include_scores=True)

    def _retrieve(self, record: DocumentRecord, query: str) -> list[Document]:
        with self._lock:
            retriever = self._cache.get(record.index_id)
        if retriever is None:
            per_doc = self.config.get("WORKSPACE_RETRIEVAL_PER_DOC_K", 8)
            config = dict(self.config, RERANKER_ENABLED=False, RETRIEVAL_INCLUDE_SCORES=True, RETRIEVAL_TOP_K=per_doc,
                          RETRIEVAL_CANDIDATE_K=max(per_doc, self.config.get("RETRIEVAL_CANDIDATE_K", 20)))
            retriever = build_retriever(self.load_index(record), config)
            with self._lock:
                self._cache[record.index_id] = retriever
        hits = retriever.invoke(query)
        result = []
        for hit in hits:
            metadata = dict(hit.metadata)
            metadata.update(document_id=record.id, document_name=record.display_name,
                            page_count=record.page_count, source_type="workspace")
            metadata.setdefault("chunk_id", hashlib.sha256(f"{record.index_id}:{metadata.get('page')}:{hit.page_content}".encode()).hexdigest()[:24])
            result.append(Document(page_content=hit.page_content, metadata=metadata))
        return result

    def invoke(self, query: str, *, coverage_query: str | None = None, document_ids: list[str] | None = None) -> list[Document]:
        if not query.strip():
            return []
        records = self.store.documents(self.workspace_id)
        if document_ids is not None:
            known = {r.id for r in records}
            if not document_ids or set(document_ids)-known:
                raise ValueError('Unknown targeted workspace document')
            records = [r for r in records if r.id in document_ids]
        futures = []
        deadline = time.monotonic() + self.config.get("WORKSPACE_RETRIEVAL_TIMEOUT_SECONDS", 30)
        # Admission is bounded across concurrent runs; timed-out work cannot accumulate indefinitely.
        for record in records:
            if not self._slots.acquire(timeout=max(0, deadline - time.monotonic())):
                LOGGER.warning("Workspace retrieval admission timeout document_id=%s", record.id)
                break
            future = self._executor.submit(self._retrieve, record, query)
            future.add_done_callback(lambda _: self._slots.release())
            futures.append((record, future))
        done, pending = wait([future for _, future in futures], timeout=max(0, deadline - time.monotonic())) if futures else (set(), set())
        rankings = []
        for record, future in futures:
            if future not in done:
                future.cancel()
                LOGGER.warning("Workspace retrieval timeout document_id=%s", record.id)
                continue
            try:
                rankings.append(future.result())
            except Exception as error:
                LOGGER.warning("Workspace document retrieval failed document_id=%s error_type=%s", record.id, type(error).__name__)
        # Round-robin is the fair fallback; raw scores from different indexes are never compared.
        merged, seen = [], set()
        for rank in range(max((len(hits) for hits in rankings), default=0)):
            for hits in rankings:
                if rank < len(hits):
                    hit = hits[rank]
                    key = (hit.metadata["document_id"], hit.metadata["chunk_id"])
                    if key not in seen:
                        seen.add(key)
                        merged.append(hit)
        if self.reranker and merged:
            try:
                merged = self.reranker.rerank(query, merged)
            except Exception as error:
                LOGGER.warning("Global reranker failed; retaining fair candidate order error_type=%s", type(error).__name__)
                if not self.config.get("RERANKER_FALLBACK", True):
                    raise
        targets = comparison_targets(coverage_query or query, records)
        selected = select_coverage_hits(merged, targets, self.config.get("WORKSPACE_RETRIEVAL_GLOBAL_K", 5))
        missing = targets - {hit.metadata["document_id"] for hit in selected}
        if missing:
            LOGGER.warning("Comparison coverage incomplete workspace_id=%s missing_document_count=%s", self.workspace_id, len(missing))
        return selected
