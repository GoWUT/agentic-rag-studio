from langchain_huggingface import HuggingFaceEmbeddings


def get_embedder(model_name: str):
    """Prefer the local model cache and download only on first use."""
    try:
        return HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs={"local_files_only": True},
        )
    except OSError:
        return HuggingFaceEmbeddings(model_name=model_name)
