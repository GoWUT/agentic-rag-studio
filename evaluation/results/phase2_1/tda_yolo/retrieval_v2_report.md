# Retrieval Evaluation V2 Report

Run ID: `retrieval-v2-20260907T113141Z`

## Environment

- Timestamp: 2026-09-07T11:31:41.297643+00:00
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

- PDF: `.rag_workspace\1327dde30bcd5c41.pdf`
- SHA-256: `1327dde30bcd5c41d52778ee7b7396a6f433f1040ccb87f75604aa3c0c8952f2`
- Size: 24475296 bytes
- Pages: 17
- Chunks: 93
- Shared index: `.runtime\retrieval-v2-phase2_1\chroma_1327dde30bcd5c41d52778ee7b7396a6f433f1040ccb87f75604aa3c0c8952f2_f78ec797f5a5`

## Evaluation Dataset

- Dataset: `evaluation\datasets\retrieval_v2_tda_yolo.jsonl`
- SHA-256: `78c8610f4264c429e53e65afa1e764347a185bf9bb9958ee0896ac5d69ff4e9c`
- Queries: 20
- Gold label: human-selected answer anchor located in native PDF text
- Categories: {"lexical": 5, "mixed": 5, "numeric": 5, "semantic": 5}
- Gold-page distribution: {"3": 3, "4": 1, "7": 1, "9": 4, "10": 4, "11": 3, "13": 1, "14": 3}
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
| Dense | 0.4000 | 0.7500 | 0.7500 | 0.7500 | 0.5500 | 0.6012 | 6.43 | 8.58 |
| BM25 | 0.5500 | 0.7000 | 0.9500 | 0.9500 | 0.6767 | 0.7436 | 0.06 | 0.09 |
| Hybrid RRF | 0.5000 | 0.7000 | 0.7500 | 0.7500 | 0.5875 | 0.6281 | 7.83 | 9.65 |
| Hybrid + Reranker | 0.4500 | 0.8500 | 0.9500 | 0.9500 | 0.6583 | 0.7323 | 1414.74 | 1691.77 |

## 95% Bootstrap Confidence Intervals

| Method | Hit@5 | MRR@5 | NDCG@5 |
| --- | ---: | ---: | ---: |
| Dense | 0.7500 [0.5500, 0.9000] | 0.5500 [0.3750, 0.7252] | 0.6012 [0.4327, 0.7710] |
| BM25 | 0.9500 [0.8500, 1.0000] | 0.6767 [0.5267, 0.8375] | 0.7436 [0.6094, 0.8746] |
| Hybrid RRF | 0.7500 [0.5500, 0.9500] | 0.5875 [0.4082, 0.7792] | 0.6281 [0.4500, 0.8065] |
| Hybrid + Reranker | 0.9500 [0.8500, 1.0000] | 0.6583 [0.5083, 0.7959] | 0.7323 [0.6038, 0.8459] |

## Latency

- BM25 index build: 4.74 ms
- Reranker cold start: 1028.41 ms
- Reranker warm-up queries: 3

| Method | Dense ms | BM25 ms | RRF ms | Reranker ms | Total ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dense | 6.43 | — | — | — | 6.43 |
| BM25 | — | 0.06 | — | — | 0.06 |
| Hybrid RRF | 7.47 | 0.11 | 0.16 | — | 7.83 |
| Hybrid + Reranker | 9.22 | 0.14 | 0.20 | 1405.16 | 1414.74 |

## Candidate Recall

- Dense candidate Hit@20: 0.9000
- Hybrid RRF candidate Hit@20: 0.9500

## Per-category Results

| Category | Dense MRR | BM25 MRR | Hybrid MRR | Hybrid + Reranker MRR |
| --- | ---: | ---: | ---: | ---: |
| lexical | 0.5667 | 0.6167 | 0.7667 | 0.7667 |
| mixed | 0.3667 | 0.5500 | 0.2667 | 0.4167 |
| numeric | 0.5000 | 0.8400 | 0.6500 | 0.9000 |
| semantic | 0.7667 | 0.7000 | 0.6667 | 0.5500 |

## Failed Cases

### Dense Miss Hybrid Success

- `tda_q01`: What weakness of conventional convolution choices motivates TDA-YOLO for complex aerial scenes? (requires manual analysis)
- `tda_q06`: How large is the VisDrone2019 image collection used in the experiments? (requires manual analysis)

### Hybrid Miss Reranker Success

- `tda_q02`: How does LAWDS preserve useful information during downsampling? (requires manual analysis)
- `tda_q08`: Why is VisDrone2019 appropriate for testing performance in practical UAV scenes? (requires manual analysis)
- `tda_q11`: With all three proposed modules enabled, what precision and mAP scores are reported on VisDrone and DOTAv2? (requires manual analysis)
- `tda_q20`: What accuracy, parameter count, and compute does TDA-YOLO report against UAV-domain methods on VisDrone2019-val? (requires manual analysis)

### Dense Success Hybrid Fail

- `tda_q02`: How does LAWDS preserve useful information during downsampling? (requires manual analysis)
- `tda_q20`: What accuracy, parameter count, and compute does TDA-YOLO report against UAV-domain methods on VisDrone2019-val? (requires manual analysis)

### Hybrid Success Reranker Fail

- None observed.

## Observations

- Hybrid RRF versus Dense: Hit@5 delta +0.0000, MRR@5 delta +0.0375, NDCG@5 delta +0.0269, and P95 latency delta +1.07 ms.
- Hybrid + Reranker versus Hybrid RRF: Hit@5 delta +0.2000, MRR@5 delta +0.0708, NDCG@5 delta +0.1043, and P95 latency delta +1682.12 ms.
- Hybrid + Reranker versus Dense: Hit@5 delta +0.2000, MRR@5 delta +0.1083, NDCG@5 delta +0.1312, and P95 latency delta +1683.18 ms.

## Known Limitations

- The benchmark contains one 17-page PDF and 20 evaluation queries.
- Relevance is binary and page-level; it does not judge passage completeness within a page.
- Query categories and answer anchors were manually assigned for this dataset.
- Warm latency was measured on this CPU host and should not be generalized to other hardware.
- The recorded reranker cold start may include model download when weights are not cached.

These measurements describe this corpus, dataset, hardware, and fixed production configuration. Confidence intervals are bootstrap intervals over evaluation queries and do not establish statistical significance between methods.
