"""Download open-access PDFs and extract their text."""

from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfReader

from .http import HttpClient

MAX_PDF_BYTES = 60 * 1024 * 1024


class FullTextError(Exception):
    pass


def download_pdf(http: HttpClient, url: str, dest: Path) -> Path:
    """Fetch ``url`` and save it to ``dest`` only if the body really is a PDF."""
    resp = http.get(url, stream=True, allow_redirects=True)
    chunks, size = [], 0
    for chunk in resp.iter_content(64 * 1024):
        size += len(chunk)
        if size > MAX_PDF_BYTES:
            raise FullTextError(f"PDF larger than {MAX_PDF_BYTES // 2**20} MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data.lstrip()[:5].startswith(b"%PDF"):
        ctype = resp.headers.get("Content-Type", "?")
        raise FullTextError(f"URL did not return a PDF (Content-Type: {ctype}); "
                            "it is probably a landing or paywall page")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def extract_text(pdf: Path | bytes) -> str:
    """Extract text page by page, marking page breaks as ``[page N]``."""
    reader = PdfReader(io.BytesIO(pdf) if isinstance(pdf, bytes) else str(pdf))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # malformed pages should not sink the whole paper
            text = ""
        pages.append(f"[page {i}]\n{clean_text(text)}")
    text = "\n\n".join(pages)
    if len(re.sub(r"\[page \d+\]|\s", "", text)) < 500:
        raise FullTextError("PDF has almost no extractable text (scanned image?)")
    return text


def clean_text(text: str) -> str:
    text = re.sub(r"-\n(?=[a-z])", "", text)        # re-join hyphenated words
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
