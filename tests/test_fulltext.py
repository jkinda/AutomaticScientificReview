from pathlib import Path

import pytest

from scireview.fulltext import FullTextError, download_pdf, extract_text

SAMPLE_PDF = next(Path(__file__).resolve().parents[1].glob("Permaebility_of_cementitious_materials/*.pdf"))


class FakeResponse:
    def __init__(self, body, ctype):
        self.body, self.headers = body, {"Content-Type": ctype}

    def iter_content(self, n):
        yield self.body


class FakeHttp:
    def __init__(self, body, ctype="application/pdf"):
        self.resp = FakeResponse(body, ctype)

    def get(self, url, **kw):
        return self.resp


def test_extract_text_from_real_paper():
    text = extract_text(SAMPLE_PDF)
    assert text.startswith("[page 1]")
    assert "water permeability" in text.lower()


def test_download_rejects_html_paywall_page(tmp_path):
    http = FakeHttp(b"<html>Sign in to read</html>", "text/html")
    with pytest.raises(FullTextError, match="did not return a PDF"):
        download_pdf(http, "https://x", tmp_path / "a.pdf")
    assert not (tmp_path / "a.pdf").exists()


def test_download_saves_pdf(tmp_path):
    http = FakeHttp(SAMPLE_PDF.read_bytes())
    path = download_pdf(http, "https://x", tmp_path / "sub" / "a.pdf")
    assert path.read_bytes()[:4] == b"%PDF"
