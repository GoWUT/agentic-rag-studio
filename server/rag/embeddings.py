from langchain_huggingface import HuggingFaceEmbeddings
from server.rag.model_runtime import MODEL_LOAD_LOCK, cached_model_path

_EMBEDDERS = {}


def get_embedder(model_name: str):
    """Prefer the local model cache and download only on first use."""
    # Transformers construction changes global initialization contexts. Concurrent
    # construction can leave another model with unmaterialized meta tensors.
    with MODEL_LOAD_LOCK:
        if model_name not in _EMBEDDERS:
            try:
                model = HuggingFaceEmbeddings(model_name=cached_model_path(model_name), model_kwargs={"local_files_only": True})
            except OSError:
                model = HuggingFaceEmbeddings(model_name=model_name)
            _EMBEDDERS[model_name] = model
        return _EMBEDDERS[model_name]
