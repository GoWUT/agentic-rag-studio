import hashlib

def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]
