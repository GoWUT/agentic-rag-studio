from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import Chroma


CHROMA_DATABASE_FILE = "chroma.sqlite3"

def build_vectorstore(
    documents,
    embedder,
    persist_dir: str,
    chunk_size: int = 800,
    chunk_overlap: int = 150,
):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    splits = splitter.split_documents(documents)

    vectordb = Chroma.from_documents(
        splits,
        embedder,
        persist_directory=persist_dir,
    )

    return vectordb


def vectorstore_exists(persist_dir: str) -> bool:
    """Return whether a persisted Chroma collection exists on disk."""
    from pathlib import Path

    return (Path(persist_dir) / CHROMA_DATABASE_FILE).is_file()


def load_vectorstore(embedder, persist_dir: str):
    """Open an existing Chroma collection without re-embedding the PDF."""
    if not vectorstore_exists(persist_dir):
        raise FileNotFoundError(
            f"No persisted Chroma database found in {persist_dir}"
        )

    return Chroma(
        persist_directory=persist_dir,
        embedding_function=embedder,
    )
