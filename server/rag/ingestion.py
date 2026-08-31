from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import RLock
from typing import Any, BinaryIO, Iterator, Protocol
import uuid

from pypdf import PdfReader


LOGGER = logging.getLogger(__name__)


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds the configured byte limit."""


class InvalidPDFError(ValueError):
    """Raised when uploaded bytes are not a usable PDF document."""


class IndexBuildError(RuntimeError):
    """Raised when a validated PDF cannot be turned into an index."""


class IndexAdapter(Protocol):
    def exists(self, index_dir: Path) -> bool: ...

    def build(self, pdf_path: Path, index_dir: Path) -> None: ...

    def load(self, index_dir: Path) -> Any: ...


class ChromaIndexAdapter:
    """Bridge the ingestion pipeline to the project's Chroma RAG stack."""

    def __init__(
        self,
        embedding_model: str,
        *,
        chunk_size: int = 800,
        chunk_overlap: int = 150,
    ):
        self.embedding_model = embedding_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        fingerprint_payload = json.dumps(
            {
                "format_version": 1,
                "embedding_model": embedding_model,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
            },
            sort_keys=True,
        ).encode("utf-8")
        self.index_suffix = "_" + hashlib.sha256(
            fingerprint_payload
        ).hexdigest()[:12]

    def exists(self, index_dir: Path) -> bool:
        from server.rag.vectorstore import vectorstore_exists

        return vectorstore_exists(str(index_dir))

    def build(self, pdf_path: Path, index_dir: Path) -> None:
        project_root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "server.rag.index_worker",
                "--pdf-path",
                str(pdf_path),
                "--index-dir",
                str(index_dir),
                "--embedding-model",
                self.embedding_model,
                "--chunk-size",
                str(self.chunk_size),
                "--chunk-overlap",
                str(self.chunk_overlap),
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            LOGGER.error(
                "PDF index worker failed: %s",
                completed.stderr[-4_000:],
            )
            raise RuntimeError("PDF index worker failed")

    def load(self, index_dir: Path) -> Any:
        from server.rag.embeddings import get_embedder
        from server.rag.vectorstore import load_vectorstore

        return load_vectorstore(
            get_embedder(self.embedding_model),
            str(index_dir),
        )


@dataclass(frozen=True)
class IngestionResult:
    file_id: str
    file_name: str
    size_bytes: int
    pdf_path: Path
    index_dir: Path
    vectorstore: Any
    status: str
    reused: bool


@dataclass(frozen=True)
class IndexStatus:
    file_id: str
    file_name: str
    size_bytes: int
    status: str
    error: str | None
    created_at: str
    updated_at: str


class _IndexStatusStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS indexes (
                    file_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    owner_pid INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(indexes)"
                ).fetchall()
            }
            if "owner_pid" not in columns:
                connection.execute(
                    "ALTER TABLE indexes ADD COLUMN owner_pid INTEGER"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def set(
        self,
        *,
        file_id: str,
        file_name: str,
        size_bytes: int,
        status: str,
        error: str | None = None,
        owner_pid: int | None = None,
    ) -> IndexStatus:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO indexes (
                    file_id, file_name, size_bytes, status, error, owner_pid,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    file_name = excluded.file_name,
                    size_bytes = excluded.size_bytes,
                    status = excluded.status,
                    error = excluded.error,
                    owner_pid = excluded.owner_pid,
                    updated_at = excluded.updated_at
                """,
                (
                    file_id,
                    file_name,
                    size_bytes,
                    status,
                    error,
                    owner_pid,
                    now,
                    now,
                ),
            )
        record = self.get(file_id)
        if record is None:  # pragma: no cover - SQLite write contract
            raise RuntimeError("Index status was not persisted")
        return record

    def get(self, file_id: str) -> IndexStatus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM indexes WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        if row is None:
            return None
        return IndexStatus(
            file_id=row["file_id"],
            file_name=row["file_name"],
            size_bytes=row["size_bytes"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def fail_interrupted(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT file_id, owner_pid
                FROM indexes
                WHERE status IN ('validating', 'indexing')
                """
            ).fetchall()
            for row in rows:
                owner_pid = row["owner_pid"]
                if (
                    owner_pid is not None
                    and owner_pid != os.getpid()
                    and _process_is_running(owner_pid)
                ):
                    continue
                connection.execute(
                    """
                    UPDATE indexes
                    SET status = 'failed',
                        error = ?,
                        owner_pid = NULL,
                        updated_at = ?
                    WHERE file_id = ?
                    """,
                    (
                        "Indexing was interrupted before completion",
                        datetime.now(timezone.utc).isoformat(),
                        row["file_id"],
                    ),
                )


class DocumentIngestionPipeline:
    """Receive a PDF and make its retrieval index available atomically."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        max_upload_bytes: int,
        index_adapter: IndexAdapter | None = None,
    ):
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        self.workspace = Path(workspace).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.index_adapter = index_adapter
        self.staging_root = self.workspace / ".ingestion"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_abandoned_staging()
        self._statuses = _IndexStatusStore(
            self.workspace / "indexes.sqlite3"
        )
        self._statuses.fail_interrupted()
        self._locks_guard = RLock()
        self._file_locks: dict[str, RLock] = {}

    def ingest(
        self,
        *,
        file_name: str,
        stream: BinaryIO,
    ) -> IngestionResult:
        safe_name = Path(file_name or "").name
        if not safe_name.lower().endswith(".pdf"):
            raise InvalidPDFError("Only a valid PDF file is supported")

        operation_dir = self.staging_root / str(uuid.uuid4())
        operation_dir.mkdir(parents=True)
        (operation_dir / "owner.pid").write_text(
            str(os.getpid()),
            encoding="ascii",
        )
        upload_path = operation_dir / "upload.pdf"
        file_id: str | None = None
        size_bytes = 0

        try:
            size_bytes, file_id = self._receive(stream, upload_path)
            file_id = self._resolve_file_id(file_id)
            self._set_status(
                file_id,
                safe_name,
                size_bytes,
                "validating",
            )
            self._validate_pdf(upload_path)
            if self.index_adapter is None:
                raise RuntimeError("An index adapter is required")

            with self._file_lock(file_id):
                pdf_path = self.workspace / f"{file_id}.pdf"
                index_suffix = getattr(
                    self.index_adapter,
                    "index_suffix",
                    "",
                )
                if Path(index_suffix).name != index_suffix:
                    raise RuntimeError("Invalid index namespace")
                index_dir = self.workspace / f"chroma_{file_id}{index_suffix}"
                if self.index_adapter.exists(index_dir):
                    if not pdf_path.exists():
                        upload_path.replace(pdf_path)
                    result = IngestionResult(
                        file_id=file_id,
                        file_name=safe_name,
                        size_bytes=size_bytes,
                        pdf_path=pdf_path,
                        index_dir=index_dir,
                        vectorstore=self.index_adapter.load(index_dir),
                        status="ready",
                        reused=True,
                    )
                    self._set_status(
                        file_id,
                        safe_name,
                        size_bytes,
                        "ready",
                    )
                    return result

                self._set_status(
                    file_id,
                    safe_name,
                    size_bytes,
                    "indexing",
                )
                created_pdf = False
                if not pdf_path.exists():
                    upload_path.replace(pdf_path)
                    created_pdf = True

                try:
                    staged_index_dir = operation_dir / "index"
                    self.index_adapter.build(pdf_path, staged_index_dir)
                    if index_dir.exists():
                        raise RuntimeError(
                            "An incomplete index already exists"
                        )
                    staged_index_dir.replace(index_dir)
                    result = IngestionResult(
                        file_id=file_id,
                        file_name=safe_name,
                        size_bytes=size_bytes,
                        pdf_path=pdf_path,
                        index_dir=index_dir,
                        vectorstore=self.index_adapter.load(index_dir),
                        status="ready",
                        reused=False,
                    )
                except Exception as error:
                    if created_pdf and pdf_path.exists():
                        pdf_path.unlink()
                    self._set_status(
                        file_id,
                        safe_name,
                        size_bytes,
                        "failed",
                        error="Failed to build PDF index",
                    )
                    raise IndexBuildError(
                        "Failed to build PDF index"
                    ) from error

                self._set_status(
                    file_id,
                    safe_name,
                    size_bytes,
                    "ready",
                )
                return result
        except UploadTooLargeError:
            raise
        except IndexBuildError:
            raise
        except Exception as error:
            if file_id is not None:
                self._set_status(
                    file_id,
                    safe_name,
                    size_bytes,
                    "failed",
                    error=(
                        str(error)
                        if isinstance(error, InvalidPDFError)
                        else "Failed to build PDF index"
                    ),
                )
            if isinstance(error, InvalidPDFError):
                raise
            raise IndexBuildError(
                "Failed to build PDF index"
            ) from error
        finally:
            self._remove_staging_directory(operation_dir)

    def get_status(self, file_id: str) -> IndexStatus | None:
        return self._statuses.get(file_id)

    def _set_status(
        self,
        file_id: str,
        file_name: str,
        size_bytes: int,
        status: str,
        *,
        error: str | None = None,
    ) -> IndexStatus:
        return self._statuses.set(
            file_id=file_id,
            file_name=file_name,
            size_bytes=size_bytes,
            status=status,
            error=error,
            owner_pid=(
                os.getpid()
                if status in {"validating", "indexing"}
                else None
            ),
        )

    def _receive(
        self,
        stream: BinaryIO,
        upload_path: Path,
    ) -> tuple[int, str]:
        received_bytes = 0
        digest = hashlib.sha256()
        with upload_path.open("wb") as destination:
            while chunk := stream.read(64 * 1024):
                received_bytes += len(chunk)
                if received_bytes > self.max_upload_bytes:
                    raise UploadTooLargeError(
                        "Uploaded PDF exceeds the limit of "
                        f"{self.max_upload_bytes} bytes"
                    )
                destination.write(chunk)
                digest.update(chunk)
        return received_bytes, digest.hexdigest()

    def _validate_pdf(self, upload_path: Path) -> None:
        if upload_path.stat().st_size == 0:
            raise InvalidPDFError("Uploaded file is not a valid PDF")

        with upload_path.open("rb") as source:
            header = source.read(1_024)
        if b"%PDF-" not in header:
            raise InvalidPDFError("Uploaded file is not a valid PDF")

        try:
            reader = PdfReader(str(upload_path), strict=False)
            if reader.is_encrypted:
                raise InvalidPDFError(
                    "Encrypted PDF files are not supported"
                )
            if len(reader.pages) == 0:
                raise InvalidPDFError("Uploaded file is not a valid PDF")
            for page in reader.pages:
                _ = page.mediabox
        except InvalidPDFError:
            raise
        except Exception as error:
            raise InvalidPDFError(
                "Uploaded file is not a valid PDF"
            ) from error

    def _file_lock(self, file_id: str) -> RLock:
        with self._locks_guard:
            return self._file_locks.setdefault(file_id, RLock())

    def _resolve_file_id(self, full_digest: str) -> str:
        legacy_file_id = full_digest[:16]
        legacy_pdf = self.workspace / f"{legacy_file_id}.pdf"
        legacy_index = self.workspace / f"chroma_{legacy_file_id}"
        if legacy_pdf.is_file():
            digest = hashlib.sha256()
            with legacy_pdf.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() == full_digest:
                return legacy_file_id
        elif legacy_index.exists():
            return legacy_file_id
        return full_digest

    def _remove_staging_directory(self, path: Path) -> None:
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(self.staging_root.resolve()):
            raise RuntimeError("Refusing to remove a path outside staging")
        shutil.rmtree(resolved_path, ignore_errors=True)

    def _cleanup_abandoned_staging(self) -> None:
        for path in self.staging_root.iterdir():
            if path.is_dir():
                owner_file = path / "owner.pid"
                try:
                    owner_pid = int(
                        owner_file.read_text(encoding="ascii").strip()
                    )
                except (FileNotFoundError, OSError, ValueError):
                    owner_pid = None
                if (
                    owner_pid is not None
                    and owner_pid != os.getpid()
                    and _process_is_running(owner_pid)
                ):
                    continue
                self._remove_staging_directory(path)
            else:
                resolved_path = path.resolve()
                if not resolved_path.is_relative_to(
                    self.staging_root.resolve()
                ):
                    raise RuntimeError(
                        "Refusing to remove a path outside staging"
                    )
                resolved_path.unlink(missing_ok=True)


def _process_is_running(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
