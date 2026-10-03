"""SQLite document registry; indexes remain owned by the ingestion pipeline."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from pydantic import BaseModel, Field


class WorkspaceNotFoundError(KeyError):
    """The requested workspace or document is absent."""


class Workspace(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    created_at: str
    updated_at: str


class DocumentRecord(BaseModel):
    id: str
    workspace_id: str
    filename: str
    display_name: str
    fingerprint: str
    index_id: str
    status: str
    page_count: int | None = None
    created_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


from server.persistence import Database


class WorkspaceStore(Database):
    """Transactional registry in the existing sessions database, without index copies."""

    def __init__(self, database_path: Path | str, max_documents: int = 20):
        super().__init__(database_path)
        self.max_documents = max_documents
        if self.backend == 'postgresql':
            return
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS workspace_documents (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                filename TEXT NOT NULL, display_name TEXT NOT NULL, fingerprint TEXT NOT NULL,
                index_id TEXT NOT NULL, status TEXT NOT NULL, page_count INTEGER,
                created_at TEXT NOT NULL, metadata TEXT NOT NULL,
                UNIQUE(workspace_id, fingerprint, index_id))""")

    def create(self, name: str, description: str = "") -> Workspace:
        now = datetime.now(timezone.utc).isoformat()
        record = Workspace(id=str(uuid4()), name=name.strip(), description=description,
                           created_at=now, updated_at=now)
        with self.connect() as db:
            db.execute("INSERT INTO workspaces (id,name,description,created_at,updated_at) VALUES (?, ?, ?, ?, ?)", tuple(record.model_dump().values()))
        return record

    def list(self) -> list[Workspace]:
        with self.connect() as db:
            return [Workspace(**dict(row)) for row in db.execute("SELECT * FROM workspaces ORDER BY updated_at DESC")]

    def get(self, workspace_id: str) -> Workspace:
        with self.connect() as db:
            row = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None:
            raise WorkspaceNotFoundError(workspace_id)
        return Workspace(**dict(row))

    def delete(self, workspace_id: str) -> None:
        self.get(workspace_id)
        with self.connect() as db:
            db.execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))

    def documents(self, workspace_id: str) -> list[DocumentRecord]:
        self.get(workspace_id)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM workspace_documents WHERE workspace_id=? ORDER BY created_at, id", (workspace_id,)).fetchall()
        return [self._document(row) for row in rows]

    def register(self, record: DocumentRecord) -> DocumentRecord:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM workspaces WHERE id=?", (record.workspace_id,)).fetchone() is None:
                raise WorkspaceNotFoundError(record.workspace_id)
            existing = db.execute("SELECT * FROM workspace_documents WHERE workspace_id=? AND fingerprint=? AND index_id=?",
                                  (record.workspace_id, record.fingerprint, record.index_id)).fetchone()
            if existing:
                return self._document(existing)
            count = db.execute("SELECT count(*) FROM workspace_documents WHERE workspace_id=?", (record.workspace_id,)).fetchone()[0]
            if count >= self.max_documents:
                raise ValueError("Workspace document limit reached")
            values = record.model_dump()
            values["metadata"] = json.dumps(record.metadata, ensure_ascii=False)
            db.execute("INSERT INTO workspace_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", tuple(values.values()))
            db.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (record.created_at, record.workspace_id))
        return record

    def delete_document(self, workspace_id: str, document_id: str) -> None:
        self.get(workspace_id)
        with self.connect() as db:
            cursor = db.execute("DELETE FROM workspace_documents WHERE workspace_id=? AND id=?", (workspace_id, document_id))
            if not cursor.rowcount:
                raise WorkspaceNotFoundError(document_id)
        # Shared PDF/index files are deliberately retained for legacy sessions and other workspaces.

    @staticmethod
    def _document(row: sqlite3.Row) -> DocumentRecord:
        values = dict(row)
        values["metadata"] = json.loads(values["metadata"])
        return DocumentRecord(**values)
