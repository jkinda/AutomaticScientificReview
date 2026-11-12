"""The review's working state: every paper seen, screening decisions and evidence.

State is saved to ``<out_dir>/state.json`` after every change, so an interrupted
run can be resumed and every decision the agent made can be audited.
"""

from __future__ import annotations

import csv
import json
import re
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Paper, ascii_slug

STATUSES = ("candidate", "included", "excluded")


@dataclass
class Entry:
    paper: Paper
    status: str = "candidate"
    reason: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    fulltext_note: str = ""  # why full text is unavailable, if it is


class Library:
    def __init__(self, out_dir: Path, topic: str = "") -> None:
        self.out_dir = Path(out_dir)
        self.topic = topic
        self.entries: dict[str, Entry] = {}
        self._by_identity: dict[str, str] = {}
        self.searches: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ papers
    def add(self, paper: Paper) -> tuple[str, bool]:
        """Add a paper or merge it into a known one. Returns (key, is_new)."""
        ident = paper.identity()
        key = self._by_identity.get(ident)
        if key is None and paper.doi:  # same work may have been indexed by title first
            key = self._by_identity.get(Paper(title=paper.title).identity())
        if key is not None:
            self.entries[key].paper.merge(paper)
            self._by_identity[ident] = key
            return key, False
        key = self._new_key(paper)
        paper.key = key
        self.entries[key] = Entry(paper=paper)
        self._by_identity[ident] = key
        self._by_identity[Paper(title=paper.title).identity()] = key
        return key, True

    def _new_key(self, paper: Paper) -> str:
        surname = ascii_slug(paper.authors[0].split()[-1]) if paper.authors else ""
        base = (surname or ascii_slug(paper.title.split()[0]) or "Anon") + str(paper.year or "nd")
        if base not in self.entries:
            return base
        for suffix in string.ascii_lowercase:
            if base + suffix not in self.entries:
                return base + suffix
        return f"{base}_{len(self.entries)}"

    def get(self, key: str) -> Entry:
        if key not in self.entries:
            raise KeyError(f"Unknown paper key '{key}'. Use a key returned by search_literature.")
        return self.entries[key]

    def with_status(self, status: str) -> list[Entry]:
        return [e for e in self.entries.values() if e.status == status]

    def counts(self) -> dict[str, int]:
        return {s: len(self.with_status(s)) for s in STATUSES} | {"total": len(self.entries)}

    # ---------------------------------------------------------------- decisions
    def screen(self, key: str, decision: str, reason: str) -> None:
        if decision not in ("included", "excluded"):
            raise ValueError("decision must be 'included' or 'excluded'")
        entry = self.get(key)
        entry.status, entry.reason = decision, reason
        self.save()

    def add_evidence(self, key: str, evidence: dict[str, Any]) -> None:
        entry = self.get(key)
        if entry.status != "included":
            raise ValueError(f"{key} is not included; screen it as included first.")
        entry.evidence.append(evidence)
        self.save()

    def log_search(self, source: str, query: str, n_results: int, n_new: int,
                   error: str | None = None) -> None:
        self.searches.append({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source, "query": query, "results": n_results, "new": n_new,
            **({"error": error} if error else {}),
        })

    # ------------------------------------------------------------- persistence
    @property
    def state_path(self) -> Path:
        return self.out_dir / "state.json"

    def save(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "topic": self.topic,
            "searches": self.searches,
            "entries": [{"paper": e.paper.to_dict(), "status": e.status, "reason": e.reason,
                         "evidence": e.evidence, "fulltext_note": e.fulltext_note}
                        for e in self.entries.values()],
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    @classmethod
    def load(cls, out_dir: Path) -> "Library":
        lib = cls(out_dir)
        data = json.loads(lib.state_path.read_text(encoding="utf-8"))
        lib.topic = data.get("topic", "")
        lib.searches = data.get("searches", [])
        for raw in data.get("entries", []):
            paper = Paper.from_dict(raw["paper"])
            lib.entries[paper.key] = Entry(paper=paper, status=raw["status"],
                                           reason=raw.get("reason", ""),
                                           evidence=raw.get("evidence", []),
                                           fulltext_note=raw.get("fulltext_note", ""))
            lib._by_identity[paper.identity()] = paper.key
            lib._by_identity[Paper(title=paper.title).identity()] = paper.key
        return lib

    # ------------------------------------------------------------------ exports
    def evidence_dossier(self) -> list[dict[str, Any]]:
        """Everything the synthesis step needs about the included papers."""
        return [{
            "key": e.paper.key, "title": e.paper.title, "authors": e.paper.authors[:6],
            "year": e.paper.year, "venue": e.paper.venue, "citations": e.paper.citations,
            "abstract": e.paper.abstract, "inclusion_reason": e.reason,
            "full_text_read": not e.fulltext_note and bool(e.evidence),
            "evidence": e.evidence,
        } for e in self.with_status("included")]

    def write_csv(self, path: Path) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["key", "status", "reason", "title", "authors", "year", "venue",
                        "doi", "url", "citations", "sources", "n_evidence"])
            for e in self.entries.values():
                p = e.paper
                w.writerow([p.key, e.status, e.reason, p.title, "; ".join(p.authors), p.year,
                            p.venue, p.doi or "", p.url or "", p.citations,
                            ",".join(p.sources), len(e.evidence)])

    def write_bibtex(self, path: Path, keys: list[str] | None = None) -> None:
        keys = keys if keys is not None else [e.paper.key for e in self.with_status("included")]
        path.write_text("\n".join(bibtex_entry(self.get(k).paper) for k in keys),
                        encoding="utf-8")


def _bib_escape(text: str) -> str:
    return re.sub(r"([{}&%$#_])", r"\\\1", text)


def bibtex_entry(p: Paper) -> str:
    kind = "misc" if p.venue in ("", "arXiv") else "article"
    fields = {
        "title": "{" + _bib_escape(p.title) + "}",
        "author": " and ".join(_bib_escape(a) for a in p.authors) or None,
        "year": str(p.year) if p.year else None,
        "journal" if kind == "article" else "howpublished": _bib_escape(p.venue) or None,
        "doi": p.doi.replace("https://doi.org/", "") if p.doi else None,
        "eprint": p.arxiv_id,
        "url": p.url,
    }
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
    return f"@{kind}{{{p.key},\n{body}\n}}\n"


def format_reference(p: Paper) -> str:
    """A plain-text reference built from metadata, never from model output."""
    authors = p.authors
    if len(authors) > 3:
        names = ", ".join(authors[:3]) + ", et al."
    else:
        names = ", ".join(authors) or "Anonymous"
    parts = [f"{names} ({p.year or 'n.d.'}). {p.title}."]
    if p.venue:
        parts.append(f"*{p.venue}*.")
    if p.doi:
        parts.append("https://doi.org/" + p.doi.replace("https://doi.org/", ""))
    elif p.url:
        parts.append(p.url)
    return " ".join(parts)
