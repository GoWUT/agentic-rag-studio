"""Process-wide serialization for transformer model initialization."""
from threading import RLock

MODEL_LOAD_LOCK = RLock()


def cached_model_path(model_name):
    """Use a snapshot path to keep cached tokenizer initialization truly local."""
    from pathlib import Path
    if Path(model_name).is_dir():
        return model_name
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError, HFValidationError
    try:
        return snapshot_download(model_name, local_files_only=True)
    except (LocalEntryNotFoundError, HFValidationError):
        return model_name
