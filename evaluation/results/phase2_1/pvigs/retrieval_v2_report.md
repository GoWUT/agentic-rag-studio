# Retrieval Evaluation V2 Report

Run ID: `retrieval-v2-20260907T113428Z`

## Environment

- Timestamp: 2026-09-07T11:34:28.366678+00:00
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

- PDF: `.rag_workspace\b4acc699ab746317.pdf`
- SHA-256: `b4acc699ab746317be68ea9f58eebafc450d7e537aba0b06f8ec2218da7287ae`
- Size: 8360482 bytes
- Pages: 16
- Chunks: 150
- Shared index: `.runtime\retrieval-v2-phase2_1\chroma_b4acc699ab746317be68ea9f58eebafc450d7e537aba0b06f8ec2218da7287ae_f78ec797f5a5`

## Evaluation Dataset

- Dataset: `evaluation\datasets\retrieval_v2_pvigs.jsonl`
- SHA-256: `eb4a7fe751bf6bb5ce99ea44854cbfe973efdc98bca97f299916aa8f60b7c3c2`
- Queries: 20
- Gold label: human-selected answer anchor located in native PDF text
- Categories: {"lexical": 5, "mixed": 5, "numeric": 5, "semantic": 5}
- Gold-page distribution: {"1": 2, "2": 1, "3": 2, "6": 3, "7": 1, "8": 4, "9": 1, "10": 2, "11": 1, "12": 2, "13": 1}
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
| Dense | 0.1000 | 0.4000 | 0.5500 | 0.5500 | 0.2575 | 0.3299 | 6.15 | 8.40 |
| BM25 | 0.5500 | 0.8000 | 0.9000 | 0.9000 | 0.6808 | 0.7355 | 0.11 | 0.25 |
| Hybrid RRF | 0.2500 | 0.8000 | 0.8500 | 0.8500 | 0.5183 | 0.6033 | 7.36 | 9.67 |
| Hybrid + Reranker | 0.5000 | 0.7500 | 0.8000 | 0.8000 | 0.6292 | 0.6727 | 1431.82 | 1773.73 |

## 95% Bootstrap Confidence Intervals

| Method | Hit@5 | MRR@5 | NDCG@5 |
| --- | ---: | ---: | ---: |
| Dense | 0.5500 [0.3500, 0.7500] | 0.2575 [0.1375, 0.4017] | 0.3299 [0.1852, 0.4799] |
| BM25 | 0.9000 [0.7500, 1.0000] | 0.6808 [0.5158, 0.8433] | 0.7355 [0.5929, 0.8759] |
| Hybrid RRF | 0.8500 [0.7000, 1.0000] | 0.5183 [0.3783, 0.6667] | 0.6033 [0.4658, 0.7358] |
| Hybrid + Reranker | 0.8000 [0.6000, 0.9500] | 0.6292 [0.4500, 0.7918] | 0.6727 [0.4946, 0.8316] |

## Latency

- BM25 index build: 6.62 ms
- Reranker cold start: 1130.91 ms
- Reranker warm-up queries: 3

| Method | Dense ms | BM25 ms | RRF ms | Reranker ms | Total ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Dense | 6.15 | — | — | — | 6.15 |
| BM25 | — | 0.11 | — | — | 0.11 |
| Hybrid RRF | 6.87 | 0.19 | 0.18 | — | 7.36 |
| Hybrid + Reranker | 8.99 | 0.21 | 0.20 | 1423.68 | 1431.82 |

## Candidate Recall

- Dense candidate Hit@20: 0.9000
- Hybrid RRF candidate Hit@20: 1.0000

## Per-category Results

| Category | Dense MRR | BM25 MRR | Hybrid MRR | Hybrid + Reranker MRR |
| --- | ---: | ---: | ---: | ---: |
| lexical | 0.1000 | 0.4400 | 0.4667 | 0.6000 |
| mixed | 0.2233 | 0.6333 | 0.5000 | 0.7000 |
| numeric | 0.2667 | 1.0000 | 0.5400 | 0.8000 |
| semantic | 0.4400 | 0.6500 | 0.5667 | 0.4167 |

## Failed Cases

### Dense Miss Hybrid Success

- `pvigs_q01`: What failure mode does standard 3D Gaussian Splatting exhibit with too few input views? (requires manual analysis)
- `pvigs_q06`: What IQA threshold and filtering-rate range are used for the four NVS datasets? (requires manual analysis)
- `pvigs_q08`: Which two rendered attributes are constrained against the visibility prior for stable sparse-view training? (requires manual analysis)
- `pvigs_q10`: On which four benchmark datasets is PViGS evaluated? (requires manual analysis)
- `pvigs_q13`: What throughput, training time, memory, and image-quality scores are listed for PViGS on LLFF? (requires manual analysis)
- `pvigs_q17`: What three reconstruction metrics are reported for PViGS on DTU with three views? (requires manual analysis)
- `pvigs_q20`: What future distillation approach is proposed to reduce dependence on pixel-level depth priors? (requires manual analysis)

### Hybrid Miss Reranker Success

- `pvigs_q04`: How does the added Gaussian visibility weight combine the two view directions? (requires manual analysis)

### Dense Success Hybrid Fail

- `pvigs_q04`: How does the added Gaussian visibility weight combine the two view directions? (requires manual analysis)

### Hybrid Success Reranker Fail

- `pvigs_q10`: On which four benchmark datasets is PViGS evaluated? (requires manual analysis)
- `pvigs_q13`: What throughput, training time, memory, and image-quality scores are listed for PViGS on LLFF? (requires manual analysis)

## Observations

- Hybrid RRF versus Dense: Hit@5 delta +0.3000, MRR@5 delta +0.2608, NDCG@5 delta +0.2734, and P95 latency delta +1.27 ms.
- Hybrid + Reranker versus Hybrid RRF: Hit@5 delta -0.0500, MRR@5 delta +0.1108, NDCG@5 delta +0.0695, and P95 latency delta +1764.07 ms.
- Hybrid + Reranker versus Dense: Hit@5 delta +0.2500, MRR@5 delta +0.3717, NDCG@5 delta +0.3429, and P95 latency delta +1765.34 ms.

## Known Limitations

- The benchmark contains one 16-page PDF and 20 evaluation queries.
- Relevance is binary and page-level; it does not judge passage completeness within a page.
- Query categories and answer anchors were manually assigned for this dataset.
- Warm latency was measured on this CPU host and should not be generalized to other hardware.
- The recorded reranker cold start may include model download when weights are not cached.

These measurements describe this corpus, dataset, hardware, and fixed production configuration. Confidence intervals are bootstrap intervals over evaluation queries and do not establish statistical significance between methods.
