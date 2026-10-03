import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from server.config import load_config
from server.rag.ingestion import ChromaIndexAdapter
from server.rag.loaders import load_pdf
from server.rag.ocr import OCRSettings, PaddlePageOCR, needs_ocr


class PDFOCRTest(unittest.TestCase):
    def extract(self, pages, settings, recognized='OCR answer'):
        reader = SimpleNamespace(pages=[Mock(extract_text=Mock(return_value=text)) for text in pages])
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'input.pdf'
            path.write_bytes(b'fixture')
            with patch('server.rag.loaders.PdfReader', return_value=reader), patch('server.rag.loaders.PaddlePageOCR') as factory:
                engine = factory.return_value.__enter__.return_value
                if isinstance(recognized, Exception):
                    engine.extract.side_effect = recognized
                else:
                    engine.extract.return_value = recognized
                documents = load_pdf(str(path), ocr_settings=settings)
                return documents, engine, reader

    def test_auto_preserves_native_and_ocr_only_sparse_pages(self):
        native = 'This is a normal searchable PDF page containing enough native text.'
        docs, engine, _ = self.extract([native, ''], OCRSettings())
        self.assertEqual([d.page_content for d in docs], [native, 'OCR answer'])
        self.assertEqual([d.metadata['page'] for d in docs], [0, 1])
        self.assertEqual([d.metadata['extraction_method'] for d in docs], ['native', 'paddleocr'])
        self.assertEqual(engine.extract.call_args.args[1], 1)
        engine.extract.assert_called_once()

    def test_always_bypasses_native_layer_and_does_not_duplicate_text(self):
        docs, engine, reader = self.extract(['native'], OCRSettings(mode='always'))
        self.assertEqual(docs[0].page_content, 'OCR answer')
        reader.pages[0].extract_text.assert_not_called()

    def test_off_never_runs_ocr_and_preserves_physical_page_numbers(self):
        docs, engine, _ = self.extract(['', 'answer'], OCRSettings(mode='off'))
        engine.extract.assert_not_called()
        self.assertEqual(docs[0].metadata['page'], 1)
        self.assertEqual(docs[0].metadata['total_pages'], 2)

    def test_empty_ocr_keeps_short_native_text_in_auto(self):
        docs, _, _ = self.extract(['Title'], OCRSettings(), recognized='')
        self.assertEqual(docs[0].page_content, 'Title')
        self.assertEqual(docs[0].metadata['extraction_method'], 'native')

    def test_no_text_rejected_and_ocr_errors_are_not_silently_skipped(self):
        with self.assertRaisesRegex(ValueError, 'no extractable text'):
            self.extract([''], OCRSettings(), recognized='')
        with self.assertRaisesRegex(RuntimeError, 'page 1.*offline'):
            self.extract([''], OCRSettings(), recognized=RuntimeError('offline'))

    def test_native_extraction_error_uses_ocr_in_auto(self):
        page = Mock()
        page.extract_text.side_effect = ValueError('broken font map')
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'input.pdf'
            path.write_bytes(b'fixture')
            with patch('server.rag.loaders.PdfReader', return_value=SimpleNamespace(pages=[page])), patch('server.rag.loaders.PaddlePageOCR') as factory:
                factory.return_value.__enter__.return_value.extract.return_value = 'recovered'
                with self.assertLogs('server.rag.loaders', level='WARNING'):
                    docs = load_pdf(str(path))
                self.assertEqual(docs[0].page_content, 'recovered')

    def test_worker_passes_ocr_settings_to_loader(self):
        from server.rag.index_worker import main
        import json
        args = ['worker', '--pdf-path', 'sample.pdf', '--index-dir', 'index',
                '--embedding-model', 'model', '--ocr-settings', json.dumps({'mode': 'always'})]
        with patch('sys.argv', args), patch('server.rag.index_worker.load_pdf', return_value=['doc']) as loader, patch('server.rag.index_worker.get_embedder', return_value='embedder'), patch('server.rag.index_worker.build_vectorstore') as build:
            main()
        self.assertEqual(loader.call_args.kwargs['ocr_settings'].mode, 'always')
        self.assertEqual(build.call_args.kwargs['documents'], ['doc'])

    def test_session_manager_uses_configured_ocr_settings(self):
        from server.sessions import AgentSessionManager
        with TemporaryDirectory() as folder:
            manager = AgentSessionManager({
                'WORKSPACE_DIR': folder, 'EMBEDDING_MODEL': 'unused',
                'PDF_OCR_MODE': 'always', 'PDF_OCR_DPI': 300,
            })
            settings = manager.ingestion_pipeline.index_adapter.ocr_settings
            self.assertEqual(settings.mode, 'always')
            self.assertEqual(settings.dpi, 300)

    def test_quality_heuristic_handles_garbled_and_mixed_text(self):
        self.assertTrue(needs_ocr('good ' * 50 + '\ufffd' * 50, 40))
        self.assertTrue(needs_ocr('... --- !!!', 40))
        self.assertFalse(needs_ocr('\u4e2d\u6587ABC123' * 20, 40))

    def test_invalid_ocr_config_rejected(self):
        for values in [
            {'PDF_OCR_MODE': 'unknown'}, {'PDF_OCR_DPI': '1000'},
            {'PDF_OCR_MIN_CONFIDENCE': 'nan'}, {'PDF_OCR_MIN_CONFIDENCE': '1.1'},
            {'PDF_OCR_MIN_TEXT_CHARS': '0'}, {'PDF_OCR_LANG': ''},
        ]:
            with self.subTest(values=values), patch('server.config.load_dotenv'), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError):
                    load_config()

    def test_ocr_config_changes_index_namespace_and_reaches_worker(self):
        settings = OCRSettings(mode='always', dpi=300, lang='en')
        adapter = ChromaIndexAdapter('model', ocr_settings=settings)
        self.assertNotEqual(adapter.index_suffix, ChromaIndexAdapter('model').index_suffix)
        self.assertEqual(adapter.index_suffix, ChromaIndexAdapter('model', ocr_settings=settings).index_suffix)
        with patch('server.rag.ingestion.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            adapter.build(Path('sample.pdf'), Path('index'))
        import json
        args = run.call_args.args[0]
        actual = OCRSettings(**json.loads(args[args.index('--ocr-settings') + 1]))
        self.assertEqual(actual, settings)
        self.assertEqual(run.call_args.kwargs['timeout'], 600)

    def test_engine_filters_low_confidence_and_releases_render_handles(self):
        import numpy as np
        engine = Mock()
        engine.predict.return_value = [{'rec_texts': ['good', 'bad', 'nan'], 'rec_scores': [0.95, 0.1, float('nan')]}]
        pdf, page, bitmap = MagicMock(), Mock(), Mock()
        pdf.__getitem__.return_value = page
        page.get_size.return_value = (600, 800)
        page.render.return_value = bitmap
        bitmap.to_numpy.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
        with PaddlePageOCR(OCRSettings()) as ocr:
            ocr._engine, ocr._pdf = engine, pdf
            self.assertEqual(ocr.extract('unused', 0), 'good')
        bitmap.close.assert_called_once()
        page.close.assert_called_once()
        pdf.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
