# Retrieval evaluation datasets

Phase 2.1 evaluates three papers with 20 manually reviewed queries per paper:

| Corpus ID | Paper | PDF pages | Queries | Local PDF | SHA-256 |
| --- | --- | ---: | ---: | --- | --- |
| gcnet | *Golden Cudgel Network for Real-Time Semantic Segmentation* | 13 | 20 | `.rag_workspace/0dad13b35659a0d6.pdf` | `0dad13b35659a0d61eb3fa3db415145b55ce89004ffc9b9b97cfb0ea8a369bb9` |
| tda_yolo | *TDA-YOLO: A Novel Adaptive YOLO-Based Framework for UAV Remote Sensing Image Object Detection with Enhanced Sampling and Detect Head Mechanisms* | 17 | 20 | `.rag_workspace/1327dde30bcd5c41.pdf` | `1327dde30bcd5c41d52778ee7b7396a6f433f1040ccb87f75604aa3c0c8952f2` |
| pvigs | *PViGS: Sparse-view 3D Gaussian splatting with pixelwise visibility-aware regularization* | 16 | 20 | `.rag_workspace/b4acc699ab746317.pdf` | `b4acc699ab746317be68ea9f58eebafc450d7e537aba0b06f8ec2218da7287ae` |

Each JSONL record contains a natural-language query, an exact answer anchor from
the PDF, and one manual category: `lexical`, `semantic`, `mixed`, or
`numeric`. Every paper has five queries in each category, producing 15 queries
per category and 60 queries overall.

The benchmark locates every anchor in native PDF text before retrieval and uses
the resulting physical page as the binary gold label. Gold pages are never
derived from retriever output. The Phase 2.1 audit found no duplicate query,
duplicate anchor, or failed anchor location across the three datasets.

`retrieval_v2_corpora.json` is the reproducible corpus manifest. It pins each
dataset to a local PDF path and SHA-256 digest. PDF files remain local and are
excluded from Git, so reproduction requires the exact revisions named above.

Run the complete evaluation with:

```bash
python -m evaluation.benchmark_retrieval_v2_multi
```

The multi-corpus runner delegates every paper to the unchanged
`evaluation.benchmark_retrieval_v2.run_benchmark` implementation, then
recomputes aggregate metrics and bootstrap intervals from all 60 per-query
results.
