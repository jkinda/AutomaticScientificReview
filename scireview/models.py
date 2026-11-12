"""Core data types shared by the search clients, the library and the agent."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Paper:
    """One bibliographic record, normalised across sources."""

    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    venue: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    citations: int | None = None
    sources: list[str] = field(default_factory=list)
    key: str = ""  # citation key assigned by the Library, e.g. "Zhang2018"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Paper":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def identity(self) -> str:
        """A key used to detect the same work returned by different sources."""
        if self.doi:
            return "doi:" + normalize_doi(self.doi)
        if self.arxiv_id:
            return "arxiv:" + re.sub(r"v\d+$", "", self.arxiv_id.lower())
        return "title:" + normalize_title(self.title)

    def merge(self, other: "Paper") -> None:
        """Fill gaps in this record with data from another record of the same work."""
        for name in ("abstract", "venue", "doi", "arxiv_id", "url", "pdf_url", "year"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if len(other.abstract) > len(self.abstract):
            self.abstract = other.abstract
        if not self.authors and other.authors:
            self.authors = other.authors
        if other.citations is not None:
            self.citations = max(self.citations or 0, other.citations)
        for s in other.sources:
            if s not in self.sources:
                self.sources.append(s)

    def short(self, abstract_chars: int = 400) -> dict[str, Any]:
        """Compact view handed to the model in search results."""
        abstract = self.abstract.strip()
        if len(abstract) > abstract_chars:
            abstract = abstract[:abstract_chars].rsplit(" ", 1)[0] + " …"
        return {
            "key": self.key,
            "title": self.title,
            "first_author": self.authors[0] if self.authors else None,
            "year": self.year,
            "venue": self.venue or None,
            "citations": self.citations,
            "open_access_pdf": bool(self.pdf_url),
            "abstract": abstract or None,
        }


def normalize_doi(doi: str) -> str:
    doi = doi.strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi)


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def ascii_slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "", text)
