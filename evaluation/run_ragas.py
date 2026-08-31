"""
Offline RAG evaluation using RAGAS with Groq (Moonshot Kimi-K2) as judge.

Best practice:
- Offline only
- No FastAPI / Streamlit imports
- Groq used only for evaluation
"""

import json
from pathlib import Path

from datasets import Dataset

from ragas import evaluate
from ragas.metrics import (
    ContextRelevance,
    Faithfulness,
    AnswerRelevancy,
)

from langchain_groq import ChatGroq
from langchain_huggingface import HuggingFaceEmbeddings

from dotenv import load_dotenv

# -----------------------------
# Load environment
# -----------------------------
load_dotenv()

# -----------------------------
# Configuration
# -----------------------------
DATASET_PATH = Path(__file__).parent / "datasets" / "rag_samples.jsonl"

# Groq judge model
JUDGE_LLM = ChatGroq(
    model="moonshotai/kimi-k2-instruct-0905",
    temperature=0,
    n=1,
)


EMBEDDINGS = HuggingFaceEmbeddings(
    model_name="BAAI/bge-small-zh-v1.5"
)


METRICS = [
    ContextRelevance(),
    Faithfulness(),
    AnswerRelevancy(),
]

# -----------------------------
# Load dataset
# -----------------------------
def load_jsonl(path: Path) -> Dataset:
    records = []

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    if not records:
        raise ValueError("Dataset is empty")

    return Dataset.from_list(records)


# -----------------------------
# Main evaluation
# -----------------------------
def main():
    print("📊 Loading RAG evaluation dataset...")
    dataset = load_jsonl(DATASET_PATH)

    print(f"✅ Loaded {len(dataset)} samples")
    print("🔍 Running RAGAS evaluation with Groq judge...\n")

    results = evaluate(
        dataset,
        metrics=METRICS,
        llm=JUDGE_LLM,
        embeddings=EMBEDDINGS,  
    )

    print("📈 RAGAS RESULTS")
    print("-" * 40)

    print(results)

    print(results.to_pandas())


    print("\n✅ Evaluation complete")


if __name__ == "__main__":
    main()
