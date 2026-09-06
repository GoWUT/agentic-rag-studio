"""Optional, local PaddleOCR 3.x extraction for individual PDF pages."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class OCRSettings:
    mode: str = 'auto'
    lang: str = 'ch'
    device: str = 'cpu'
    dpi: int = 200
    min_text_chars: int = 40
    min_confidence: float = 0.5

    def __post_init__(self):
        if self.mode not in {'auto', 'always', 'off'}:
            raise ValueError('PDF_OCR_MODE must be auto, always, or off')
        if not 72 <= self.dpi <= 400:
            raise ValueError('PDF_OCR_DPI must be between 72 and 400')
        if self.min_text_chars <= 0:
            raise ValueError('PDF_OCR_MIN_TEXT_CHARS must be positive')
        if not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise ValueError('PDF_OCR_MIN_CONFIDENCE must be between 0 and 1')
        if not self.lang.strip() or not self.device.strip():
            raise ValueError('PDF_OCR_LANG and PDF_OCR_DEVICE cannot be empty')

    @classmethod
    def from_config(cls, config):
        return cls(**{
            name: config.get('PDF_OCR_' + name.upper(), field.default)
            for name, field in cls.__dataclass_fields__.items()
        })


def needs_ocr(text: str, minimum: int) -> bool:
    visible = [character for character in text if not character.isspace()]
    if sum(character.isalnum() for character in visible) < minimum:
        return True
    suspicious = sum(
        character == '\ufffd' or (not character.isprintable())
        for character in visible
    )
    return suspicious / max(1, len(visible)) > 0.05


class PaddlePageOCR:
    """Load models only when needed; release PDF rendering handles promptly."""
    def __init__(self, settings: OCRSettings):
        self.settings = settings
        self._engine = None
        self._pdf = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self._pdf is not None:
            self._pdf.close()

    def extract(self, pdf_path: str, page_index: int) -> str:
        if self._engine is None:
            try:
                from paddleocr import PaddleOCR
                import pypdfium2 as pdfium
            except ImportError as error:
                raise RuntimeError(
                    'PaddleOCR dependencies are missing. Run uv sync --extra ocr '
                    'or set PDF_OCR_MODE=off for text-only PDFs.'
                ) from error
            self._engine = PaddleOCR(
                lang=self.settings.lang,
                device=self.settings.device,
                # Avoid Paddle 3.3 oneDNN/PIR conversion failures on CPU.
                enable_mkldnn=False,
                ocr_version='PP-OCRv5',
                use_doc_orientation_classify=True,
                use_doc_unwarping=False,
                use_textline_orientation=True,
            )
            self._pdf = pdfium.PdfDocument(pdf_path)
        page = self._pdf[page_index]
        try:
            width, height = page.get_size()
            # Bound raster memory even for unusually large PDF page dimensions.
            scale = min(self.settings.dpi / 72, math.sqrt(16_000_000 / (width * height)))
            bitmap = page.render(scale=scale, force_bitmap_format=2)
            try:
                # PDFium format 2 is BGR, the ndarray format PaddleOCR expects.
                image = bitmap.to_numpy().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
        lines = []
        for result in self._engine.predict(input=image):
            texts, scores = result['rec_texts'], result['rec_scores']
            if len(texts) != len(scores):
                raise ValueError('PaddleOCR returned inconsistent text/score counts')
            for text, score in zip(texts, scores):
                score = float(score)
                if math.isfinite(score) and score >= self.settings.min_confidence and text.strip():
                    lines.append(text.strip())
        return '\n'.join(lines)
