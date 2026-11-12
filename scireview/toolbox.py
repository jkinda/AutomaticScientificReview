"""The review operations, shared by both ways of running the agent.

* The local agent (``scireview.agent``) exposes them as Claude tools.
* The hosted Managed Agent calls them from its sandbox through
  ``python -m scireview.tools`` (``scireview.tools``).

Every operation re-reads ``state.json`` under an exclusive file lock, changes it
and saves it, so several agents working in parallel on the same review (as in a
Managed Agents workflow run) do not overwrite each other's decisions.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .fulltext import download_pdf, extract_text
from .http import HttpClient
from .library import Library
from .sources import SEARCHERS, SOURCES

READ_CHUNK = 15_000


class ToolError(Exception):
    """An error to report back to the model rather than crash on."""


class ToolBox:
    def __init__(self, out_dir: Path, topic: str = "", year_from: int | None = None,
                 year_to: int | None = None, http: HttpClient | None = None,
                 log: Callable[[str], None] = print) -> None:
        self.out_dir = Path(out_dir)
        self.year_from, self.year_to = year_from, year_to
        self._http = http
        self.log = log
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with self.library() as lib:
            if topic and not lib.topic:
                lib.topic = topic
            # The year window is kept in the state so every caller (and every parallel
            # agent) searches with the same one.
            if year_from is not None:
                lib.settings["year_from"] = year_from
            if year_to is not None:
                lib.settings["year_to"] = year_to
            self.year_from = lib.settings.get("year_from")
            self.year_to = lib.settings.get("year_to")

    @property
    def http(self) -> HttpClient:
        if self._http is None:
            self._http = HttpClient()
        return self._http

    @contextmanager
    def library(self, write: bool = True) -> Iterator[Library]:
        """Load the library under a lock; save it on exit when ``write`` is true."""
        with open(self.out_dir / "state.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                lib = (Library.load(self.out_dir) if (self.out_dir / "state.json").exists()
                       else Library(self.out_dir))
                yield lib
                if write:
                    lib.save()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    # ------------------------------------------------------------- operations
    def search(self, query: str, source: str = "openalex", limit: int = 20) -> dict[str, Any]:
        if source not in SEARCHERS:
            raise ToolError(f"source must be one of {list(SOURCES)}")
        limit = max(1, min(int(limit), 50))
        try:  # network outside the lock
            papers = SEARCHERS[source](self.http, query, limit, self.year_from, self.year_to)
        except Exception as exc:
            with self.library() as lib:
                lib.log_search(source, query, 0, 0, error=str(exc)[:200])
            raise ToolError(f"{source} search failed: {exc}"[:500]) from exc
        results, n_new = [], 0
        with self.library() as lib:
            for p in papers:
                key, is_new = lib.add(p)
                n_new += is_new
                entry = lib.get(key)
                results.append(entry.paper.short() | {"status": entry.status, "new": is_new})
            lib.log_search(source, query, len(papers), n_new)
            counts = lib.counts()
        self.log(f"  search[{source}] {query!r}: {len(papers)} results, {n_new} new")
        return {"results": results, "library": counts}

    def get_paper(self, key: str) -> dict[str, Any]:
        with self.library(write=False) as lib:
            e = self._entry(lib, key)
            return e.paper.to_dict() | {"status": e.status, "reason": e.reason,
                                        "evidence": e.evidence,
                                        "full_text_problem": e.fulltext_note or None}

    def screen(self, key: str, decision: str, reason: str) -> dict[str, Any]:
        with self.library() as lib:
            self._entry(lib, key)
            try:
                lib.screen(key, decision, reason)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            counts = lib.counts()
        self.log(f"  {decision:8} {key}: {reason[:90]}")
        return {"ok": True, "library": counts}

    def read(self, key: str, start_char: int = 0) -> dict[str, Any]:
        txt_path = self.out_dir / "fulltext" / f"{key}.txt"
        if not txt_path.exists():
            with self.library(write=False) as lib:
                paper = self._entry(lib, key).paper
            if not paper.pdf_url:
                raise ToolError("No open-access PDF known for this paper; use the abstract.")
            try:  # download outside the lock: it can take a while
                pdf = download_pdf(self.http, paper.pdf_url, self.out_dir / "pdfs" / f"{key}.pdf")
                text = extract_text(pdf)
            except Exception as exc:  # FullTextError, HTTP errors, malformed PDFs
                note = f"full text unavailable: {exc}"[:300]
                with self.library() as lib:
                    lib.get(key).fulltext_note = note
                raise ToolError(note) from exc
            txt_path.parent.mkdir(parents=True, exist_ok=True)
            txt_path.write_text(text, encoding="utf-8")
            self.log(f"  read     {key}: {len(text):,} chars of full text")
        text = txt_path.read_text(encoding="utf-8")
        start = max(0, int(start_char))
        chunk = text[start:start + READ_CHUNK]
        end = start + len(chunk)
        return {"key": key, "total_chars": len(text), "start_char": start,
                "next_start_char": end if end < len(text) else None, "text": chunk}

    def record_evidence(self, key: str, study_summary: str, methods: str, key_results: str,
                        limitations: str, based_on: str = "full_text",
                        replace: bool = False) -> dict[str, Any]:
        evidence = {"study_summary": study_summary, "methods": methods,
                    "key_results": key_results, "limitations": limitations,
                    "based_on": based_on if based_on in ("full_text", "abstract") else "abstract"}
        with self.library() as lib:
            self._entry(lib, key)
            try:
                lib.add_evidence(key, evidence, replace=replace)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            counts = lib.counts()
        self.log(f"  evidence {key} ({evidence['based_on']})")
        return {"ok": True, "library": counts}

    def list(self, status: str = "included") -> dict[str, Any]:
        with self.library(write=False) as lib:
            rows = [{"key": e.paper.key, "title": e.paper.title, "year": e.paper.year,
                     "citations": e.paper.citations, "open_access_pdf": bool(e.paper.pdf_url),
                     "evidence_records": len(e.evidence),
                     "full_text_problem": e.fulltext_note or None}
                    for e in lib.with_status(status)]
            return {"status": status, "papers": rows, "counts": lib.counts()}

    @staticmethod
    def _entry(lib: Library, key: str):
        try:
            return lib.get(key)
        except KeyError as exc:
            raise ToolError(str(exc.args[0])) from exc
