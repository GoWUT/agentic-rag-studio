from io import BytesIO
import hashlib
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfWriter

from server.rag.ingestion import (
    ChromaIndexAdapter,
    DocumentIngestionPipeline,
    IndexBuildError,
    InvalidPDFError,
    UploadTooLargeError,
)


class RecordingIndexAdapter:
    def __init__(self):
        self.build_calls = 0
        self.final_index_dir: Path | None = None

    def exists(self, index_dir: Path) -> bool:
        return (index_dir / "index.bin").is_file()

    def build(self, pdf_path: Path, index_dir: Path) -> None:
        self.build_calls += 1
        if self.final_index_dir is not None:
            if self.final_index_dir.exists():
                raise AssertionError("Final index became visible too early")
        index_dir.mkdir(parents=True)
        (index_dir / "index.bin").write_bytes(b"complete")

    def load(self, index_dir: Path):
        return {"index_dir": str(index_dir)}


class FailingIndexAdapter(RecordingIndexAdapter):
    def build(self, pdf_path: Path, index_dir: Path) -> None:
        index_dir.mkdir(parents=True)
        (index_dir / "partial.bin").write_bytes(b"partial")
        raise OSError("simulated disk failure")


class InterruptedIndexAdapter(RecordingIndexAdapter):
    def build(self, pdf_path: Path, index_dir: Path) -> None:
        index_dir.mkdir(parents=True)
        (index_dir / "partial.bin").write_bytes(b"partial")
        raise KeyboardInterrupt()


def valid_pdf_bytes() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


class PDFIngestionPipelineTest(unittest.TestCase):
    def test_embedding_configuration_has_a_stable_index_namespace(self):
        first = ChromaIndexAdapter("BAAI/bge-small-zh-v1.5")
        same = ChromaIndexAdapter("BAAI/bge-small-zh-v1.5")
        different = ChromaIndexAdapter(
            "sentence-transformers/all-MiniLM-L6-v2"
        )

        self.assertEqual(first.index_suffix, same.index_suffix)
        self.assertNotEqual(first.index_suffix, different.index_suffix)

    def test_startup_removes_an_abandoned_staging_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            abandoned = workspace / ".ingestion" / "abandoned" / "index"
            abandoned.mkdir(parents=True)
            (abandoned / "partial.bin").write_bytes(b"partial")

            DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
            )

            self.assertFalse(abandoned.parent.exists())

    def test_rejects_oversized_upload_without_leaving_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pipeline = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=8,
            )

            with self.assertRaisesRegex(
                UploadTooLargeError,
                "8 bytes",
            ):
                pipeline.ingest(
                    file_name="large.pdf",
                    stream=BytesIO(b"123456789"),
                )

            self.assertEqual(list(workspace.rglob("*.pdf")), [])

    def test_rejects_a_file_that_only_fakes_the_pdf_header(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pipeline = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=1_024,
            )

            with self.assertRaisesRegex(
                InvalidPDFError,
                "valid PDF",
            ):
                pipeline.ingest(
                    file_name="fake.pdf",
                    stream=BytesIO(b"%PDF-1.7\nthis is not a real pdf"),
                )

            self.assertEqual(list(workspace.rglob("*.pdf")), [])

    def test_builds_in_staging_then_atomically_publishes_the_index(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            adapter = RecordingIndexAdapter()
            pipeline = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
                index_adapter=adapter,
            )
            pdf_bytes = valid_pdf_bytes()
            expected_file_id = hashlib.sha256(pdf_bytes).hexdigest()
            adapter.final_index_dir = (
                workspace / f"chroma_{expected_file_id}"
            )

            result = pipeline.ingest(
                file_name="paper.pdf",
                stream=BytesIO(pdf_bytes),
            )

            self.assertEqual(adapter.build_calls, 1)
            self.assertEqual(result.file_id, expected_file_id)
            self.assertEqual(result.index_dir, adapter.final_index_dir)
            self.assertEqual(result.file_name, "paper.pdf")
            self.assertEqual(result.size_bytes, len(pdf_bytes))
            self.assertEqual(
                result.pdf_path,
                workspace / f"{expected_file_id}.pdf",
            )
            self.assertEqual(result.status, "ready")
            self.assertTrue(
                (result.index_dir / "index.bin").is_file()
            )
            self.assertEqual(
                result.vectorstore,
                {"index_dir": str(result.index_dir)},
            )

    def test_failed_build_is_cleaned_and_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pdf_bytes = valid_pdf_bytes()
            file_id = hashlib.sha256(pdf_bytes).hexdigest()
            pipeline = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
                index_adapter=FailingIndexAdapter(),
            )

            with self.assertRaisesRegex(
                IndexBuildError,
                "build PDF index",
            ):
                pipeline.ingest(
                    file_name="paper.pdf",
                    stream=BytesIO(pdf_bytes),
                )

            self.assertFalse((workspace / f"chroma_{file_id}").exists())
            self.assertEqual(
                list((workspace / ".ingestion").iterdir()),
                [],
            )
            status = pipeline.get_status(file_id)
            self.assertIsNotNone(status)
            self.assertEqual(status.status, "failed")

    def test_restart_marks_an_interrupted_index_build_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            pdf_bytes = valid_pdf_bytes()
            file_id = hashlib.sha256(pdf_bytes).hexdigest()
            pipeline = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
                index_adapter=InterruptedIndexAdapter(),
            )

            with self.assertRaises(KeyboardInterrupt):
                pipeline.ingest(
                    file_name="paper.pdf",
                    stream=BytesIO(pdf_bytes),
                )
            self.assertEqual(
                pipeline.get_status(file_id).status,
                "indexing",
            )

            restarted = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
            )

            recovered = restarted.get_status(file_id)
            self.assertEqual(recovered.status, "failed")
            self.assertIn("interrupted", recovered.error.lower())

    def test_reuses_a_ready_index_and_status_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            adapter = RecordingIndexAdapter()
            pdf_bytes = valid_pdf_bytes()
            file_id = hashlib.sha256(pdf_bytes).hexdigest()
            pipeline = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
                index_adapter=adapter,
            )

            first = pipeline.ingest(
                file_name="paper.pdf",
                stream=BytesIO(pdf_bytes),
            )
            second = pipeline.ingest(
                file_name="renamed.pdf",
                stream=BytesIO(pdf_bytes),
            )
            restarted = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
                index_adapter=adapter,
            )

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(adapter.build_calls, 1)
            status = restarted.get_status(file_id)
            self.assertIsNotNone(status)
            self.assertEqual(status.status, "ready")
            self.assertEqual(status.file_name, "renamed.pdf")

    def test_reuses_an_index_created_with_the_legacy_short_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            adapter = RecordingIndexAdapter()
            pdf_bytes = valid_pdf_bytes()
            legacy_file_id = hashlib.sha256(pdf_bytes).hexdigest()[:16]
            legacy_index = workspace / f"chroma_{legacy_file_id}"
            legacy_index.mkdir()
            (legacy_index / "index.bin").write_bytes(b"complete")
            (workspace / f"{legacy_file_id}.pdf").write_bytes(pdf_bytes)
            pipeline = DocumentIngestionPipeline(
                workspace=workspace,
                max_upload_bytes=10_000,
                index_adapter=adapter,
            )

            result = pipeline.ingest(
                file_name="legacy.pdf",
                stream=BytesIO(pdf_bytes),
            )

            self.assertEqual(result.file_id, legacy_file_id)
            self.assertTrue(result.reused)
            self.assertEqual(adapter.build_calls, 0)


if __name__ == "__main__":
    unittest.main()
