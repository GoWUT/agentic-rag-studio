from __future__ import annotations

import argparse
import json

from server.rag.ocr import OCRSettings

from server.rag.embeddings import get_embedder
from server.rag.loaders import load_pdf
from server.rag.vectorstore import build_vectorstore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-path", required=True)
    parser.add_argument("--index-dir", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--chunk-overlap", type=int, default=150)
    parser.add_argument("--ocr-settings", default="{}")
    args = parser.parse_args()

    documents = load_pdf(
        args.pdf_path, ocr_settings=OCRSettings(**json.loads(args.ocr_settings)),
    )
    build_vectorstore(
        documents=documents,
        embedder=get_embedder(args.embedding_model),
        persist_dir=args.index_dir,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )


if __name__ == "__main__":
    main()
