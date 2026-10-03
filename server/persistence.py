"""Additive SQLite persistence shared by Phase 3 stores."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def identity():
    return str(uuid4())


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Database:
    def __init__(self, path):
        self.backend = getattr(path, 'backend', 'sqlite')
        self.runtime = path if self.backend == 'postgresql' else None
        self.path = path if self.runtime else Path(path)
        if not self.runtime:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        if self.runtime:
            with self.runtime.connect() as connection:
                yield connection
            return
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()


class RecordNotFound(KeyError):
    """An ID is absent or outside the requested scope."""
