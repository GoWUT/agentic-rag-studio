"""Page-preserving PDF text extraction with selective OCR."""
import logging

from langchain_core.documents import Document
from pypdf import PdfReader

from server.rag.ocr import OCRSettings, PaddlePageOCR, needs_ocr

LOGGER = logging.getLogger(__name__)


def load_pdf(pdf_path: str, *, ocr_settings: OCRSettings | None = None):
    settings = ocr_settings or OCRSettings()
    docs = []
    with open(pdf_path, 'rb') as source, PaddlePageOCR(settings) as ocr:
        reader = PdfReader(source)
        for page_number, page in enumerate(reader.pages):
            text = ''
            if settings.mode != 'always':
                try:
                    text = page.extract_text() or ''
                except Exception:
                    if settings.mode == 'off':
                        raise
                    LOGGER.warning('Native extraction failed on page %s; trying OCR', page_number + 1)
            method = 'native'
            if settings.mode == 'always' or (
                settings.mode == 'auto' and needs_ocr(text, settings.min_text_chars)
            ):
                # OCR failure must fail indexing, not silently omit scanned pages.
                try:
                    recognized = ocr.extract(pdf_path, page_number)
                except Exception as error:
                    raise RuntimeError(
                        f'PaddleOCR failed on PDF page {page_number + 1}: {error}'
                    ) from error
                if recognized.strip() or settings.mode == 'always':
                    text, method = recognized, 'paddleocr'
            if text.strip():
                docs.append(Document(page_content=text, metadata={
                    'source': str(pdf_path),
                    'page': page_number,
                    'total_pages': len(reader.pages),
                    'extraction_method': method,
                }))
    if not docs:
        raise ValueError('PDF contains no extractable text after extraction/OCR')
    return docs
