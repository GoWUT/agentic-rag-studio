"""Turn-local evidence identities and deterministic citation validation/rendering."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)


class Evidence(BaseModel):
    evidence_id: str
    source_type: Literal["workspace", "pdf", "web", "arxiv", "analysis", "mcp", "github"]
    source_metadata: dict = Field(default_factory=dict)
    execution_id: str | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    document_id: str | None = None
    document_name: str | None = None
    page: int | None = Field(default=None, ge=1)
    page_count: int | None = Field(default=None, ge=1)
    chunk_id: str | None = None
    title: str | None = None
    url: str | None = None
    content: str
    retrieval_score: float | None = None
    rerank_score: float | None = None
    extraction_method: str | None = None
    table_labels: list[str] = Field(default_factory=list)


class Citation(BaseModel):
    citation_id: str
    evidence_id: str
    document_name: str | None = None
    page: int | None = None
    title: str | None = None
    url: str | None = None
    execution_id: str | None = None
    dataset_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)


def merge_evidence_items(*groups):
    """Repeated retrieval can change scores, never the underlying source identity/content."""
    merged = {}
    for group in groups:
        for raw in group:
            item = Evidence.model_validate(raw).model_dump()
            previous = merged.get(item['evidence_id'])
            if previous:
                stable = [k for k in item if k not in {'retrieval_score', 'rerank_score'}]
                if any(previous[k] != item[k] for k in stable):
                    raise ValueError('Evidence identity/content conflict')
            merged[item['evidence_id']] = item
    return list(merged.values())


def evidence_from_hit(hit: Any, source_type: str = "pdf") -> Evidence:
    """Convert zero-based physical PDF page metadata to a one-based public page."""
    metadata = hit.metadata
    page = metadata.get("page")
    page = int(page) + 1 if page is not None else None
    chunk_id = metadata.get("chunk_id") or hashlib.sha256(hit.page_content.encode()).hexdigest()[:24]
    identity = f"{metadata.get('document_id', metadata.get('source'))}:{page}:{chunk_id}"
    return Evidence(
        evidence_id="ev_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        source_type=metadata.get("source_type", source_type),
        document_id=metadata.get("document_id"), document_name=metadata.get("document_name"),
        page=page, page_count=metadata.get("page_count", metadata.get("total_pages")),
        chunk_id=chunk_id, content=hit.page_content,
        retrieval_score=metadata.get("retrieval_score"), rerank_score=metadata.get("rerank_score"),
        extraction_method=metadata.get("extraction_method"),
        table_labels=metadata.get("table_labels", []),
    )


def external_evidence(source: str, title: str, content: str, url: str | None) -> dict[str, Any]:
    identity = hashlib.sha256(f"{source}:{url}:{title}:{content}".encode()).hexdigest()[:24]
    return Evidence(evidence_id="ev_" + identity, source_type=source, title=title,
                    content=content, url=url).model_dump()


def validate_citations(citations: list[Citation], evidence: list[Evidence],
                       documents: Mapping[str, int | None] | None = None) -> list[Citation]:
    """Reject invented identity, metadata and pages against this turn's evidence only."""
    store = {item.evidence_id: item for item in evidence}
    valid = []
    for citation in citations:
        item = store.get(citation.evidence_id)
        if item is None:
            LOGGER.warning("Rejecting citation with unknown evidence identity")
            continue
        if any(getattr(citation, field) != getattr(item, field) for field in ("document_name", "page", "title", "url", "execution_id", "dataset_ids", "artifact_ids")):
            LOGGER.warning("Rejecting citation with mismatched source metadata")
            continue
        if item.source_type in {"pdf", "workspace"}:
            if not item.document_id or not item.document_name or item.page is None:
                continue
            if documents is not None and item.document_id not in documents:
                continue
            count = documents.get(item.document_id) if documents is not None else item.page_count
            if count is None or item.page > count:
                continue
        if item.url and not re.match(r"^https?://[^\s]+$", item.url):
            continue
        valid.append(citation)
    return valid


def render_citations(answer: str, evidence: list[Evidence],
                     documents: Mapping[str, int | None] | None = None) -> tuple[str, list[Citation]]:
    """Only [ev_<id>] markers emitted by the model can become public citations."""
    by_id = {item.evidence_id: item for item in evidence}
    known_urls = {item.url for item in evidence if item.url}
    known_tables = {label.casefold() for item in evidence for label in item.table_labels}
    known_pages = {item.page for item in evidence if item.page is not None}
    known_document_pages = {(item.document_name, item.page) for item in evidence if item.document_name}
    answer = re.sub(r"([^\s\[\],;]+\.pdf)\s+(?:Page\s+|p\.\s*)(\d+)\b",
                    lambda match: match[0] if (match[1], int(match[2])) in known_document_pages else "[document/page reference unavailable]",
                    answer, flags=re.I)
    answer = re.sub(r"\bTable\s+(?:[IVXLCDM]+|\d+)\b", lambda match: match[0] if match[0].casefold() in known_tables else "[table reference unavailable]", answer, flags=re.I)
    answer = re.sub(r"https?://[^\s\])>]+", lambda match: match[0] if match[0] in known_urls else "[unverified URL removed]", answer)
    candidates = []
    for identity in dict.fromkeys(re.findall(r"\[(ev_[a-zA-Z0-9_-]+)\]", answer)):
        item = by_id.get(identity)
        if item:
            candidates.append(Citation(citation_id=str(len(candidates) + 1), evidence_id=identity,
                                       document_name=item.document_name, page=item.page,
                                       title=item.title, url=item.url, execution_id=item.execution_id,
                                       dataset_ids=item.dataset_ids, artifact_ids=item.artifact_ids))
    valid = validate_citations(candidates, evidence, documents)
    labels = {}
    counters: dict[str, int] = {}
    for citation in valid:
        item = by_id[citation.evidence_id]
        if item.source_type in {"pdf", "workspace"}:
            labels[item.evidence_id] = f"[{item.document_name}, p.{item.page}]"
        elif item.source_type == "analysis":
            labels[item.evidence_id] = f"[Analysis {item.execution_id}]"
        else:
            kind = {'web': 'Web', 'arxiv': 'arXiv', 'github': 'GitHub', 'mcp': 'MCP'}.get(item.source_type, 'MCP')
            counters[kind] = counters.get(kind, 0) + 1
            labels[item.evidence_id] = f"[{kind} {counters[kind]}]"
    # Strip fabricated numeric/source labels as well as invented evidence markers.
    answer = re.sub(r"\[(?:\d+|Web\s+\d+|arXiv\s+\d+|[^\]\n]*\.pdf[^\]\n]*|[^\]\n]*,\s*p\.\s*\d+)\]", "", answer, flags=re.I)
    answer = re.sub(r"\b(?:Page\s+|p\.\s*)(\d+)\b", lambda match: match[0] if int(match[1]) in known_pages else "[page reference unavailable]", answer, flags=re.I)
    answer = re.sub(r"\[(ev_[a-zA-Z0-9_-]+)\]", lambda match: labels.get(match[1], ""), answer)
    return answer, valid
