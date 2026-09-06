"""BM25 and dense retrieval with RRF fusion and optional cross-encoder ranking."""
from collections import Counter, defaultdict
import json
import logging
import math
import re
from threading import RLock
import unicodedata

from langchain_core.documents import Document

LOGGER = logging.getLogger(__name__)
_MODEL_LOCK = RLock()
_MODELS = {}


def tokenize(text):
    """English words and Chinese characters/bigrams; no external dictionary."""
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens = []
    for part in re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", text):
        if "\u3400" <= part[0] <= "\u9fff":
            tokens.extend(part)
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            tokens.append(part)
    return tokens


def _key(document):
    # LangChain's Chroma search adapter may omit the record ID.
    return (document.page_content, json.dumps(
        document.metadata, sort_keys=True, ensure_ascii=False, default=str,
    ))


class BM25Index:
    """Immutable per-PDF Okapi BM25 index (k1=1.5, b=0.75)."""
    def __init__(self, documents):
        self.documents = list(documents)
        self.postings = defaultdict(list)
        self.lengths = []
        for index, document in enumerate(self.documents):
            counts = Counter(tokenize(document.page_content))
            self.lengths.append(sum(counts.values()))
            for term, frequency in counts.items():
                self.postings[term].append((index, frequency))
        self.average_length = sum(self.lengths) / max(1, len(self.lengths))

    def search(self, query, k):
        if not self.average_length:
            return []
        scores = defaultdict(float)
        for term in sorted(set(tokenize(query))):
            posting = self.postings.get(term, [])
            idf = math.log1p(
                (len(self.documents) - len(posting) + 0.5) / (len(posting) + 0.5)
            )
            for index, frequency in posting:
                norm = 1.5 * (0.25 + 0.75 * self.lengths[index] / self.average_length)
                scores[index] += idf * frequency * 2.5 / (frequency + norm)
        order = sorted(scores, key=lambda index: (-scores[index], index))
        return [self.documents[index] for index in order[:k]]


def reciprocal_rank_fusion(rankings, k):
    scores, documents = defaultdict(float), {}
    for ranking in rankings:
        seen = set()
        for rank, document in enumerate(ranking, 1):
            key = _key(document)
            if key in seen:
                continue
            seen.add(key)
            documents.setdefault(key, document)
            scores[key] += 1 / (60 + rank)
    order = sorted(scores, key=lambda key: -scores[key])
    return [documents[key] for key in order[:k]]


class CrossEncoderReranker:
    def __init__(self, model_name, device="cpu", batch_size=8):
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size

    def rerank(self, query, documents):
        if not documents:
            return []
        # Share one model per process/configuration and serialize inference.
        with _MODEL_LOCK:
            key = (self.model_name, self.device)
            if key not in _MODELS:
                from sentence_transformers import CrossEncoder
                _MODELS[key] = CrossEncoder(
                    self.model_name, device=self.device, max_length=512,
                )
            scores = _MODELS[key].predict(
                [(query, document.page_content) for document in documents],
                batch_size=self.batch_size, show_progress_bar=False,
            )
        if len(scores) != len(documents):
            raise ValueError("Reranker must return one score per document")
        values = [float(score) for score in scores]
        if not all(math.isfinite(score) for score in values):
            raise ValueError("Reranker returned non-finite scores")
        order = sorted(range(len(documents)), key=lambda index: -values[index])
        return [documents[index] for index in order]


class PDFRetriever:
    def __init__(self, vectorstore, *, top_k=5, candidate_k=20,
                 hybrid=True, reranker=None, fallback=True):
        if top_k <= 0 or candidate_k < top_k:
            raise ValueError("candidate_k must be >= top_k > 0")
        self.vectorstore = vectorstore
        self.top_k = top_k
        self.candidate_k = candidate_k
        self.reranker = reranker
        self.fallback = fallback
        self.bm25 = None
        if hybrid:
            records = vectorstore.get(include=["documents", "metadatas"])
            self.bm25 = BM25Index([
                Document(page_content=text, metadata=metadata or {})
                for text, metadata in zip(records["documents"], records["metadatas"])
                if text and text.strip()
            ])

    def invoke(self, query):
        if not query.strip():
            return []
        dense = self.vectorstore.similarity_search(query, k=self.candidate_k)
        rankings = [dense]
        if self.bm25 is not None:
            rankings.append(self.bm25.search(query, self.candidate_k))
        candidates = reciprocal_rank_fusion(rankings, self.candidate_k)
        if self.reranker is not None and candidates:
            try:
                candidates = self.reranker.rerank(query, candidates)
            except Exception:
                if not self.fallback:
                    raise
                LOGGER.warning("Reranker unavailable; using retrieval ranking", exc_info=True)
        return candidates[:self.top_k]


def build_retriever(vectorstore, config):
    reranker = None
    if config.get("RERANKER_ENABLED", True):
        reranker = CrossEncoderReranker(
            config.get("RERANKER_MODEL", "BAAI/bge-reranker-base"),
            config.get("RERANKER_DEVICE", "cpu"),
            config.get("RERANKER_BATCH_SIZE", 8),
        )
    return PDFRetriever(
        vectorstore,
        top_k=config.get("RETRIEVAL_TOP_K", 5),
        candidate_k=config.get("RETRIEVAL_CANDIDATE_K", 20),
        hybrid=config.get("HYBRID_RETRIEVAL_ENABLED", True),
        reranker=reranker,
        fallback=config.get("RERANKER_FALLBACK", True),
    )
