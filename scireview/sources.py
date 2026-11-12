"""Search clients for open scholarly databases.

Each ``search_*`` function returns a list of :class:`Paper`. The ``parse_*``
functions are pure (JSON/XML in, papers out) so they can be tested offline.
"""

from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Callable

from .http import HttpClient
from .models import Paper

SOURCES = ("openalex", "semantic_scholar", "crossref", "arxiv")


# --------------------------------------------------------------------------- OpenAlex

def search_openalex(http: HttpClient, query: str, limit: int, year_from: int | None,
                    year_to: int | None) -> list[Paper]:
    params: dict[str, Any] = {"search": query, "per-page": min(limit, 50)}
    filters = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    if os.environ.get("SCIREVIEW_CONTACT_EMAIL"):
        params["mailto"] = os.environ["SCIREVIEW_CONTACT_EMAIL"]
    resp = http.get("https://api.openalex.org/works", params=params)
    return parse_openalex(resp.json())


def _openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions = [(pos, word) for word, poss in inverted.items() for pos in poss]
    return " ".join(word for _, word in sorted(positions))


def parse_openalex(data: dict[str, Any]) -> list[Paper]:
    papers = []
    for w in data.get("results", []):
        title = w.get("display_name") or w.get("title")
        if not title:
            continue
        best = w.get("best_oa_location") or {}
        primary = w.get("primary_location") or {}
        source = primary.get("source") or {}
        arxiv_id = None
        ids = w.get("ids") or {}
        for loc in w.get("locations") or []:
            landing = (loc or {}).get("landing_page_url") or ""
            m = re.search(r"arxiv\.org/abs/([^\s?#]+)", landing)
            if m:
                arxiv_id = m.group(1)
        papers.append(Paper(
            title=title,
            authors=[a["author"]["display_name"] for a in w.get("authorships", [])
                     if a.get("author", {}).get("display_name")],
            year=w.get("publication_year"),
            abstract=_openalex_abstract(w.get("abstract_inverted_index")),
            venue=source.get("display_name") or "",
            doi=w.get("doi"),
            arxiv_id=arxiv_id,
            url=w.get("doi") or ids.get("openalex"),
            pdf_url=best.get("pdf_url"),
            citations=w.get("cited_by_count"),
            sources=["openalex"],
        ))
    return papers


# --------------------------------------------------------------------- Semantic Scholar

S2_FIELDS = "title,abstract,year,authors,venue,citationCount,externalIds,openAccessPdf,url"


def search_semantic_scholar(http: HttpClient, query: str, limit: int, year_from: int | None,
                            year_to: int | None) -> list[Paper]:
    params: dict[str, Any] = {"query": query, "limit": min(limit, 100), "fields": S2_FIELDS}
    if year_from or year_to:
        params["year"] = f"{year_from or ''}-{year_to or ''}"
    headers = {}
    if os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    resp = http.get("https://api.semanticscholar.org/graph/v1/paper/search",
                    params=params, headers=headers)
    return parse_semantic_scholar(resp.json())


def parse_semantic_scholar(data: dict[str, Any]) -> list[Paper]:
    papers = []
    for p in data.get("data", []) or []:
        if not p.get("title"):
            continue
        ext = p.get("externalIds") or {}
        papers.append(Paper(
            title=p["title"],
            authors=[a["name"] for a in p.get("authors") or [] if a.get("name")],
            year=p.get("year"),
            abstract=p.get("abstract") or "",
            venue=p.get("venue") or "",
            doi=ext.get("DOI"),
            arxiv_id=ext.get("ArXiv"),
            url=p.get("url"),
            pdf_url=(p.get("openAccessPdf") or {}).get("url") or None,
            citations=p.get("citationCount"),
            sources=["semantic_scholar"],
        ))
    return papers


# ----------------------------------------------------------------------------- Crossref

def search_crossref(http: HttpClient, query: str, limit: int, year_from: int | None,
                    year_to: int | None) -> list[Paper]:
    params: dict[str, Any] = {"query.bibliographic": query, "rows": min(limit, 100)}
    filters = []
    if year_from:
        filters.append(f"from-pub-date:{year_from}")
    if year_to:
        filters.append(f"until-pub-date:{year_to}")
    if filters:
        params["filter"] = ",".join(filters)
    resp = http.get("https://api.crossref.org/works", params=params)
    return parse_crossref(resp.json())


def _strip_tags(text: str) -> str:
    """Remove JATS/HTML markup and collapse whitespace."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def parse_crossref(data: dict[str, Any]) -> list[Paper]:
    papers = []
    for it in (data.get("message") or {}).get("items", []):
        titles = it.get("title") or []
        if not titles:
            continue
        year = None
        for k in ("published-print", "published-online", "issued", "created"):
            parts = (it.get(k) or {}).get("date-parts") or [[None]]
            if parts and parts[0] and parts[0][0]:
                year = parts[0][0]
                break
        pdf = next((l["URL"] for l in it.get("link") or []
                    if l.get("content-type") == "application/pdf"), None)
        authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x)
                   for a in it.get("author") or []]
        papers.append(Paper(
            title=_strip_tags(titles[0]),
            authors=[a for a in authors if a],
            year=year,
            abstract=_strip_tags(it.get("abstract") or ""),
            venue=(it.get("container-title") or [""])[0],
            doi=it.get("DOI"),
            url=it.get("URL"),
            pdf_url=pdf,
            citations=it.get("is-referenced-by-count"),
            sources=["crossref"],
        ))
    return papers


# -------------------------------------------------------------------------------- arXiv

ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def search_arxiv(http: HttpClient, query: str, limit: int, year_from: int | None,
                 year_to: int | None) -> list[Paper]:
    # Every word must appear in title or abstract; avoids the loose matching that
    # returned unrelated papers in the original notebook.
    words = [w for w in re.findall(r"[\w-]+", query) if len(w) > 2]
    search = " AND ".join(f"all:{w}" for w in words) or f'all:"{query}"'
    params = {"search_query": search, "max_results": min(limit, 100),
              "sortBy": "relevance"}
    resp = http.get("https://export.arxiv.org/api/query", params=params)
    papers = parse_arxiv(resp.text)
    return [p for p in papers
            if (not year_from or (p.year or 0) >= year_from)
            and (not year_to or (p.year or 9999) <= year_to)]


def parse_arxiv(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers = []
    for e in root.findall("a:entry", ATOM):
        title = " ".join((e.findtext("a:title", "", ATOM)).split())
        if not title or title == "Error":
            continue
        abs_url = e.findtext("a:id", "", ATOM)
        arxiv_id = abs_url.rsplit("/abs/", 1)[-1] if "/abs/" in abs_url else None
        pdf = next((l.get("href") for l in e.findall("a:link", ATOM)
                    if l.get("title") == "pdf"), None)
        published = e.findtext("a:published", "", ATOM)
        papers.append(Paper(
            title=title,
            authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
            year=int(published[:4]) if published[:4].isdigit() else None,
            abstract=" ".join(e.findtext("a:summary", "", ATOM).split()),
            venue="arXiv",
            doi=e.findtext("arxiv:doi", None, ATOM),
            arxiv_id=arxiv_id,
            url=abs_url,
            pdf_url=pdf,
            sources=["arxiv"],
        ))
    return papers


SEARCHERS: dict[str, Callable[..., list[Paper]]] = {
    "openalex": search_openalex,
    "semantic_scholar": search_semantic_scholar,
    "crossref": search_crossref,
    "arxiv": search_arxiv,
}
