"""The review agent: a Claude tool-use loop for research, then a synthesis call.

Phase 1 (research): Claude drives the search with the tools defined in
:func:`build_tools` (thin wrappers over :class:`scireview.toolbox.ToolBox`) —
searching databases, screening papers, reading full texts and recording
evidence into the review's library.

Phase 2 (synthesis): one streaming call turns the recorded evidence into a
review. Citations are checked against the library and the reference list is
built from metadata, so the bibliography cannot contain invented papers.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import anthropic
from anthropic import beta_tool

from .library import Library, format_reference
from .prompts import RESEARCH_SYSTEM, SYNTHESIS_SYSTEM
from .toolbox import ToolBox, ToolError

MODEL = "claude-opus-5-5"
# On a safety-classifier decline, the API re-runs the request on a fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Clears old tool results (long full-text pages) from the context as it grows;
# the agent keeps what matters by writing it to the library with record_evidence.
CONTEXT_BETA = "context-management-2025-06-27"
CITATION_RE = re.compile(r"\[(@[^\]]+)\]")


@dataclass
class ReviewConfig:
    topic: str
    out_dir: Path
    criteria: str = ""
    language: str = "English"
    year_from: int | None = None
    year_to: int | None = None
    target_papers: int = 25
    max_iterations: int = 80
    model: str = MODEL
    effort: str = "high"


# ------------------------------------------------------------------------------ tools

def _call(fn: Callable[..., dict[str, Any]], *args, **kwargs) -> str:
    """Run a ToolBox operation and return JSON; errors go back to the model as data."""
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except ToolError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def build_tools(box: ToolBox) -> list:
    """Expose the ToolBox operations as Claude tools."""

    @beta_tool
    def search_literature(query: str, source: str = "openalex", limit: int = 20) -> str:
        """Search a scholarly database and add the results to the review library.

        Returns compact records (key, title, first author, year, venue, citation count,
        whether an open-access PDF exists, abstract excerpt). Papers already in the
        library keep their key and are marked as seen.

        Args:
            query: A short keyword query (3-6 words work best), e.g. "laterite soil water retention curve".
            source: One of "openalex", "semantic_scholar", "crossref", "arxiv".
            limit: Number of results to return, 1-50.
        """
        return _call(box.search, query, source, limit)

    @beta_tool
    def get_paper(key: str) -> str:
        """Get the full metadata, complete abstract and recorded evidence of a paper.

        Args:
            key: The paper key returned by search_literature, e.g. "Zhang2018".
        """
        return _call(box.get_paper, key)

    @beta_tool
    def screen_paper(key: str, decision: str, reason: str) -> str:
        """Record whether a paper is included in or excluded from the review.

        Args:
            key: The paper key.
            decision: "included" or "excluded".
            reason: One specific sentence explaining the decision against the inclusion criteria.
        """
        return _call(box.screen, key, decision, reason)

    @beta_tool
    def read_full_text(key: str, start_char: int = 0) -> str:
        """Read the open-access full text of a paper, about 15,000 characters at a time.

        The text contains "[page N]" markers. Call again with start_char set to the
        returned next_start_char to continue. Fails when no open-access PDF exists;
        then rely on the abstract.

        Args:
            key: The paper key.
            start_char: Character offset to start reading from.
        """
        return _call(box.read, key, start_char)

    @beta_tool
    def record_evidence(key: str, study_summary: str, methods: str, key_results: str,
                        limitations: str, based_on: str = "full_text") -> str:
        """Save extracted evidence for an included paper. This is what the review is written from.

        Args:
            key: The paper key (must already be screened as included).
            study_summary: What was studied: materials, samples, scope, objective.
            methods: Experimental, numerical or analytical methods and standards used.
            key_results: Main findings with exact numbers, units and conditions, as reported.
            limitations: Limitations stated by the authors or evident from the method.
            based_on: "full_text" or "abstract" — what you actually read.
        """
        return _call(box.record_evidence, key, study_summary, methods, key_results,
                     limitations, based_on)

    @beta_tool
    def list_library(status: str = "included") -> str:
        """List papers in the library by status, with how much evidence each has.

        Args:
            status: "candidate", "included" or "excluded".
        """
        return _call(box.list, status)

    return [search_literature, get_paper, screen_paper, read_full_text,
            record_evidence, list_library]


# --------------------------------------------------------------------------- phase 1

def _research_brief(cfg: ReviewConfig, lib: Library) -> str:
    years = ""
    if cfg.year_from or cfg.year_to:
        years = f"\nPublication years: {cfg.year_from or 'any'} to {cfg.year_to or 'present'}."
    resume = ""
    if lib.entries:
        resume = (f"\n\nThis is a resumed run. The library already holds {lib.counts()}. "
                  "Call list_library to see where you left off, and continue from there.")
    return (f"Review topic: {cfg.topic}\n"
            f"Inclusion criteria: {cfg.criteria or 'Studies that directly address the topic.'}"
            f"{years}\nTarget: about {cfg.target_papers} included papers with recorded evidence."
            f"{resume}")


def run_research(client: anthropic.Anthropic, cfg: ReviewConfig, box: ToolBox,
                 log: Callable[[str], None] = print) -> str:
    with box.library(write=False) as lib:
        brief = _research_brief(cfg, lib)
    runner = client.beta.messages.tool_runner(
        model=cfg.model,
        max_tokens=16000,
        max_iterations=cfg.max_iterations,
        system=RESEARCH_SYSTEM,
        tools=build_tools(box),
        messages=[{"role": "user", "content": brief}],
        output_config={"effort": cfg.effort},
        cache_control={"type": "ephemeral"},
        context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
        betas=[FALLBACK_BETA, CONTEXT_BETA],
        fallbacks="default",
    )
    last = None
    for message in runner:
        last = message
        for block in message.content:
            if block.type == "text" and block.text.strip():
                log("\n" + block.text.strip() + "\n")
    if last is None:
        return ""
    if last.stop_reason == "refusal":
        raise RuntimeError(f"Research phase was declined by the model: {last.stop_details}")
    if last.stop_reason == "tool_use":
        log(f"(stopped after max_iterations={cfg.max_iterations}; "
            "re-run with --resume to continue)")
    return "\n".join(b.text for b in last.content if b.type == "text")


# --------------------------------------------------------------------------- phase 2

def synthesis_prompt(cfg: ReviewConfig, lib: Library, research_summary: str) -> str:
    counts = lib.counts()
    return "\n\n".join([
        f"Write the review in {cfg.language}. Topic: {cfg.topic}",
        f"Inclusion criteria: {cfg.criteria or 'studies that directly address the topic'}",
        f"Screening counts: {json.dumps(counts)}",
        "Search log:\n" + json.dumps(lib.searches, ensure_ascii=False),
        "Research assistant's notes:\n" + (research_summary or "(none)"),
        "<evidence_dossier>\n" + json.dumps(lib.evidence_dossier(), ensure_ascii=False,
                                            indent=1) + "\n</evidence_dossier>",
    ])


def run_synthesis(client: anthropic.Anthropic, cfg: ReviewConfig, lib: Library,
                  research_summary: str) -> str:
    with client.beta.messages.stream(
        model=cfg.model,
        max_tokens=64000,
        system=SYNTHESIS_SYSTEM,
        messages=[{"role": "user", "content": synthesis_prompt(cfg, lib, research_summary)}],
        output_config={"effort": cfg.effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError(f"Synthesis was declined by the model: {message.stop_details}")
    text = "".join(b.text for b in message.content if b.type == "text")
    if message.stop_reason == "max_tokens":
        text += "\n\n> **Note:** the review was cut off at the output limit.\n"
    return text


def check_citations(review: str, lib: Library) -> tuple[list[str], list[str]]:
    """Return (cited keys in order of first use, cited keys not in the included set)."""
    included = {e.paper.key for e in lib.with_status("included")}
    cited: list[str] = []
    for group in CITATION_RE.findall(review):
        for key in re.findall(r"@([A-Za-z0-9_]+)", group):
            if key not in cited:
                cited.append(key)
    return cited, [k for k in cited if k not in included]


def finalize(review: str, lib: Library, export_dir: Path | None = None) -> dict[str, Any]:
    """Append metadata-built references, flag bad citations and write all outputs.

    Outputs go to the library's directory, and are also copied to ``export_dir``
    when given (the hosted agent exports to /mnt/session/outputs).
    """
    cited, unknown = check_citations(review, lib)
    refs = [k for k in cited if k not in unknown]
    out = review.rstrip() + "\n\n## References\n\n"
    out += "\n".join(f"- **[{k}]** {format_reference(lib.get(k).paper)}" for k in refs) + "\n"
    if unknown:
        out += ("\n> **Citation check:** these keys are not among the included papers and "
                "should be verified: " + ", ".join(unknown) + "\n")
    d = lib.out_dir
    (d / "review.md").write_text(out, encoding="utf-8")
    lib.write_bibtex(d / "references.bib", refs)
    lib.write_csv(d / "papers.csv")
    lib.save()
    if export_dir is not None:
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        for name in ("review.md", "references.bib", "papers.csv", "state.json"):
            shutil.copyfile(d / name, export_dir / name)
    return {"cited": len(refs), "unknown_citations": unknown, "counts": lib.counts(),
            "uncited_included": [e.paper.key for e in lib.with_status("included")
                                 if e.paper.key not in refs]}
