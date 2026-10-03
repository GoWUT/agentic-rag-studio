# Retrieval Evaluation V2 Phase 2.1 Report

Run ID: retrieval-v2-phase2_1-20260907T113428Z

## Scope and Integrity

- Corpora: 3 papers, 46 pages, 344 chunks
- Queries: 60
- Categories: {"lexical": 15, "mixed": 15, "numeric": 15, "semantic": 15}
- Gold labels: manually selected answer anchors located before retrieval
- Algorithms and parameters: unchanged; every corpus uses the original single-corpus runner
- Duplicate queries / anchors: 0 / 0
- Anchor location failures: 0

## Environment

- Timestamp: 2026-09-07T11:29:00.263090+00:00
- Git commit: 24f01d5f2901191fbdcde8a2117250b0b728c93c
- Worktree dirty: True
- Platform: Windows-10-10.0.26200-SP0
- CPU: Intel64 Family 6 Model 198 Stepping 2, GenuineIntel
- GPU: none
- Python / PyTorch: 3.11.16 / 2.9.1

## Fixed Configuration

- Embedding: BAAI/bge-small-zh-v1.5
- Reranker: BAAI/bge-reranker-base on cpu
- Chunk size / overlap: 800 / 150
- Top K / candidate K / RRF constant: 5 / 20 / 60
- Repeats: 5
- Bootstrap: 2000 samples, seed 42

## Aggregate Results

| Method | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR@5 | NDCG@5 | P50 ms | P95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Dense | 0.3000 | 0.6167 | 0.6833 | 0.6833 | 0.4483 | 0.5074 | 5.77 | 8.26 |
| BM25 | 0.5500 | 0.7667 | 0.9333 | 0.9333 | 0.6836 | 0.7454 | 0.06 | 0.21 |
| Hybrid RRF | 0.4167 | 0.7833 | 0.8333 | 0.8333 | 0.5914 | 0.6528 | 6.78 | 9.29 |
| Hybrid + Reranker | 0.5167 | 0.8167 | 0.8833 | 0.8833 | 0.6750 | 0.7281 | 1447.93 | 1883.45 |

## 95% Bootstrap Confidence Intervals Across 60 Queries

| Method | Hit@5 | MRR@5 | NDCG@5 |
| --- | ---: | ---: | ---: |
| Dense | 0.6833 [0.5667, 0.8000] | 0.4483 [0.3450, 0.5531] | 0.5074 [0.4058, 0.6096] |
| BM25 | 0.9333 [0.8667, 0.9833] | 0.6836 [0.5864, 0.7714] | 0.7454 [0.6614, 0.8206] |
| Hybrid RRF | 0.8333 [0.7333, 0.9167] | 0.5914 [0.4964, 0.6839] | 0.6528 [0.5585, 0.7392] |
| Hybrid + Reranker | 0.8833 [0.8000, 0.9667] | 0.6750 [0.5819, 0.7681] | 0.7281 [0.6432, 0.8121] |

## Per-paper Results

| Corpus | Queries | Pages | Chunks | Method | Hit@5 | MRR@5 | NDCG@5 | P50 ms | P95 ms |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Golden Cudgel Network for Real-Time Semantic Segmentation | 20 | 13 | 101 | Dense | 0.7500 | 0.5375 | 0.5912 | 4.73 | 5.84 |
| Golden Cudgel Network for Real-Time Semantic Segmentation | 20 | 13 | 101 | BM25 | 0.9500 | 0.6933 | 0.7571 | 0.05 | 0.07 |
| Golden Cudgel Network for Real-Time Semantic Segmentation | 20 | 13 | 101 | Hybrid RRF | 0.9000 | 0.6683 | 0.7271 | 5.45 | 6.80 |
| Golden Cudgel Network for Real-Time Semantic Segmentation | 20 | 13 | 101 | Hybrid + Reranker | 0.9000 | 0.7375 | 0.7793 | 1599.01 | 2002.16 |
| TDA-YOLO: A Novel Adaptive YOLO-Based Framework for UAV Remote Sensing Image Object Detection with Enhanced Sampling and Detect Head Mechanisms | 20 | 17 | 93 | Dense | 0.7500 | 0.5500 | 0.6012 | 6.43 | 8.58 |
| TDA-YOLO: A Novel Adaptive YOLO-Based Framework for UAV Remote Sensing Image Object Detection with Enhanced Sampling and Detect Head Mechanisms | 20 | 17 | 93 | BM25 | 0.9500 | 0.6767 | 0.7436 | 0.06 | 0.09 |
| TDA-YOLO: A Novel Adaptive YOLO-Based Framework for UAV Remote Sensing Image Object Detection with Enhanced Sampling and Detect Head Mechanisms | 20 | 17 | 93 | Hybrid RRF | 0.7500 | 0.5875 | 0.6281 | 7.83 | 9.65 |
| TDA-YOLO: A Novel Adaptive YOLO-Based Framework for UAV Remote Sensing Image Object Detection with Enhanced Sampling and Detect Head Mechanisms | 20 | 17 | 93 | Hybrid + Reranker | 0.9500 | 0.6583 | 0.7323 | 1414.74 | 1691.77 |
| PViGS: Sparse-view 3D Gaussian splatting with pixelwise visibility-aware regularization | 20 | 16 | 150 | Dense | 0.5500 | 0.2575 | 0.3299 | 6.15 | 8.40 |
| PViGS: Sparse-view 3D Gaussian splatting with pixelwise visibility-aware regularization | 20 | 16 | 150 | BM25 | 0.9000 | 0.6808 | 0.7355 | 0.11 | 0.25 |
| PViGS: Sparse-view 3D Gaussian splatting with pixelwise visibility-aware regularization | 20 | 16 | 150 | Hybrid RRF | 0.8500 | 0.5183 | 0.6033 | 7.36 | 9.67 |
| PViGS: Sparse-view 3D Gaussian splatting with pixelwise visibility-aware regularization | 20 | 16 | 150 | Hybrid + Reranker | 0.8000 | 0.6292 | 0.6727 | 1431.82 | 1773.73 |

## Candidate Recall

- Dense candidate Hit@20: 0.9167
- Hybrid candidate Hit@20: 0.9833

## Per-category MRR@5

| Category | Dense | BM25 | Hybrid RRF | Hybrid + Reranker |
| --- | ---: | ---: | ---: | ---: |
| lexical | 0.3278 | 0.5578 | 0.6111 | 0.6889 |
| mixed | 0.4300 | 0.6611 | 0.5222 | 0.6056 |
| numeric | 0.4444 | 0.8633 | 0.6100 | 0.8667 |
| semantic | 0.5911 | 0.6522 | 0.6222 | 0.5389 |

## Retrieval Regressions and Recoveries

### Dense Miss Hybrid Success

- gcnet_q01 (gcnet): What limitations of existing real-time segmentation designs motivated the authors to create GCNet?
- gcnet_q06 (gcnet): How many 1 x 1 convolutions were selected for the dedicated path after the ablation study?
- gcnet_q20 (gcnet): At what input sizes were CamVid and VOC inference speeds measured?
- tda_q01 (tda_yolo): What weakness of conventional convolution choices motivates TDA-YOLO for complex aerial scenes?
- tda_q06 (tda_yolo): How large is the VisDrone2019 image collection used in the experiments?
- pvigs_q01 (pvigs): What failure mode does standard 3D Gaussian Splatting exhibit with too few input views?
- pvigs_q06 (pvigs): What IQA threshold and filtering-rate range are used for the four NVS datasets?
- pvigs_q08 (pvigs): Which two rendered attributes are constrained against the visibility prior for stable sparse-view training?
- pvigs_q10 (pvigs): On which four benchmark datasets is PViGS evaluated?
- pvigs_q13 (pvigs): What throughput, training time, memory, and image-quality scores are listed for PViGS on LLFF?
- pvigs_q17 (pvigs): What three reconstruction metrics are reported for PViGS on DTU with three views?
- pvigs_q20 (pvigs): What future distillation approach is proposed to reduce dependence on pixel-level depth priors?

### Hybrid Miss Reranker Success

- tda_q02 (tda_yolo): How does LAWDS preserve useful information during downsampling?
- tda_q08 (tda_yolo): Why is VisDrone2019 appropriate for testing performance in practical UAV scenes?
- tda_q11 (tda_yolo): With all three proposed modules enabled, what precision and mAP scores are reported on VisDrone and DOTAv2?
- tda_q20 (tda_yolo): What accuracy, parameter count, and compute does TDA-YOLO report against UAV-domain methods on VisDrone2019-val?
- pvigs_q04 (pvigs): How does the added Gaussian visibility weight combine the two view directions?

### Dense Success Hybrid Fail

- tda_q02 (tda_yolo): How does LAWDS preserve useful information during downsampling?
- tda_q20 (tda_yolo): What accuracy, parameter count, and compute does TDA-YOLO report against UAV-domain methods on VisDrone2019-val?
- pvigs_q04 (pvigs): How does the added Gaussian visibility weight combine the two view directions?

### Hybrid Success Reranker Fail

- pvigs_q10 (pvigs): On which four benchmark datasets is PViGS evaluated?
- pvigs_q13 (pvigs): What throughput, training time, memory, and image-quality scores are listed for PViGS on LLFF?

## Known Limitations

- The benchmark covers three computer-vision papers; other domains and languages remain untested.
- Relevance is binary and page-level; passage completeness within a page is not graded.
- Queries, categories, and answer anchors were manually authored and checked.
- Latency is host-specific and should not be generalized to other hardware.
- Bootstrap intervals over queries do not establish pairwise significance.
