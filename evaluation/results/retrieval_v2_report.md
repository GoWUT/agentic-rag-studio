# Retrieval Evaluation V2 Report

Run ID: `retrieval-v2-20260907T112900Z`

## Environment

- Timestamp: 2026-09-07T11:29:00.263090+00:00
- Git commit: `24f01d5f2901191fbdcde8a2117250b0b728c93c`
- Git worktree dirty: True
- Platform: Windows-10-10.0.26200-SP0
- CPU: Intel64 Family 6 Model 198 Stepping 2, GenuineIntel
- GPU: none
- CUDA available: False
- Python: 3.11.16
- PyTorch: 2.9.1
- sentence-transformers: 5.2.0
- Device: cpu

## Corpus

- PDF: `.rag_workspace\0dad13b35659a0d6.pdf`
- SHA-256: `0dad13b35659a0d61eb3fa3db415145b55ce89004ffc9b9b97cfb0ea8a369bb9`
- Size: 1309368 bytes
- Pages: 13
- Chunks: 101
- Shared index: `.runtime\retrieval-v2-phase2_1\chroma_0dad13b35659a0d61eb3fa3db415145b55ce89004ffc9b9b97cfb0ea8a369bb9_f78ec797f5a5`

## Evaluation Dataset

- Dataset: `evaluation\datasets\retrieval_v2_gcnet.jsonl`
- SHA-256: `1dfb88748561aa03dbab50f3a0f200249edefb9f0e3befe1c5c77172e263176a`
- Queries: 20
- Gold label: human-selected answer anchor located in native PDF text
- Categories: {"lexical": 5, "mixed": 5, "numeric": 5, "semantic": 5}
- Gold-page distribution: {"1": 3, "2": 1, "3": 1, "4": 3, "6": 3, "7": 1, "8": 3, "11": 3, "13": 2}
- Duplicate queries: 0
- Duplicate anchors: 0
- Anchor location failures: 0

## Configuration

- Embedding: `BAAI/bge-small-zh-v1.5`
- Reranker: `BAAI/bge-reranker-base`
- Chunk size / overlap: 800 / 150
- Top K / candidate K: 5 / 20
- RRF constant: 60
- Repeats: 5
- Bootstrap: 2000 samples, seed 42

## Ablation Table

| Method | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR@5 | NDCG@5 | P50 ms | P95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Dense | 0.4000 | 0.7000 | 0.7500 | 0.7500 | 0.5375 | 0.5912 | 4.73 | 5.84 |
| BM25 | 0.5500 | 0.8000 | 0.9500 | 0.9500 | 0.6933 | 0.7571 | 0.05 | 0.07 |
| Hybrid RRF | 0.5000 | 0.8500 | 0.9000 | 0.9000 | 0.6683 | 0.7271 | 5.45 | 6.80 |
| Hybrid + Reranker | 0.6000 | 0.8500 | 0.9000 | 0.9000 | 0.7375 | 0.7793 | 1599.01 | 2002.16 |

## 95% Bootstrap Confidence Intervals

| Method | Hit@5 | MRR@5 | NDCG@5 |
| --- | ---: | ---: | ---: |
| Dense | 0.7500 [0.5500, 0.9000] | 0.5375 [0.3583, 0.7167] | 0.5912 [0.4196, 0.7631] |
| BM25 | 0.9500 [0.8500, 1.0000] | 0.6933 [0.5300, 0.8459] | 0.7571 [0.6241, 0.8816] |
| Hybrid RRF | 0.9000 [0.7500, 1.0000] | 0.6683 [0.5083, 0.8183] | 0.7271 [0.5771, 0.8577] |
| Hybrid + Reranker | 0.9000 [0.7500, 1.0000] | 0.7375 [0.5875, 0.9000] | 0.7793 [0.6408, 0.9162] |

## Latency

- BM25 index build: 5.20 ms
- Reranker cold start: 5727.35 ms
- Reranker warm-up queries: 3

| Method | Dense ms | BM25 ms | RRF ms | Reranker ms | Total ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dense | 4.73 | — | — | — | 4.73 |
| BM25 | — | 0.05 | — | — | 0.05 |
| Hybrid RRF | 5.17 | 0.11 | 0.15 | — | 5.45 |
| Hybrid + Reranker | 8.32 | 0.14 | 0.18 | 1587.75 | 1599.01 |

## Candidate Recall

- Dense candidate Hit@20: 0.9500
- Hybrid RRF candidate Hit@20: 1.0000

## Per-category Results

| Category | Dense MRR | BM25 MRR | Hybrid MRR | Hybrid + Reranker MRR |
| --- | ---: | ---: | ---: | ---: |
| lexical | 0.3167 | 0.6167 | 0.6000 | 0.7000 |
| mixed | 0.7000 | 0.8000 | 0.8000 | 0.7000 |
| numeric | 0.5667 | 0.7500 | 0.6400 | 0.9000 |
| semantic | 0.5667 | 0.6067 | 0.6333 | 0.6500 |

## Failed Cases

### Dense Miss Hybrid Success

- `gcnet_q01`: What limitations of existing real-time segmentation designs motivated the authors to create GCNet? (requires manual analysis)
- `gcnet_q06`: How many 1 x 1 convolutions were selected for the dedicated path after the ablation study? (requires manual analysis)
- `gcnet_q20`: At what input sizes were CamVid and VOC inference speeds measured? (requires manual analysis)

### Hybrid Miss Reranker Success

- None observed.

### Dense Success Hybrid Fail

- None observed.

### Hybrid Success Reranker Fail

- None observed.

## Observations

- Hybrid RRF versus Dense: Hit@5 delta +0.1500, MRR@5 delta +0.1308, NDCG@5 delta +0.1359, and P95 latency delta +0.96 ms.
- Hybrid + Reranker versus Hybrid RRF: Hit@5 delta +0.0000, MRR@5 delta +0.0692, NDCG@5 delta +0.0522, and P95 latency delta +1995.37 ms.
- Hybrid + Reranker versus Dense: Hit@5 delta +0.1500, MRR@5 delta +0.2000, NDCG@5 delta +0.1881, and P95 latency delta +1996.33 ms.

## Known Limitations

- The benchmark contains one 13-page PDF and 20 evaluation queries.
- Relevance is binary and page-level; it does not judge passage completeness within a page.
- Query categories and answer anchors were manually assigned for this dataset.
- Warm latency was measured on this CPU host and should not be generalized to other hardware.
- The recorded reranker cold start may include model download when weights are not cached.

These measurements describe this corpus, dataset, hardware, and fixed production configuration. Confidence intervals are bootstrap intervals over evaluation queries and do not establish statistical significance between methods.
