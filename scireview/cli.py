"""Command-line entry point: ``python -m scireview "topic" [options]``."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date
from pathlib import Path

import anthropic

from .agent import MODEL, ReviewConfig, finalize, run_research, run_synthesis
from .http import HttpClient
from .library import Library


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].rstrip("-") or "review"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="scireview",
        description="Search the literature on a topic and write a cited review with Claude.")
    p.add_argument("topic", help="The review topic, in any language.")
    p.add_argument("--criteria", default="", help="Inclusion/exclusion criteria, in plain words.")
    p.add_argument("--language", default="English", help="Language of the written review.")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--papers", type=int, default=25, help="Target number of included papers.")
    p.add_argument("--max-iterations", type=int, default=80,
                   help="Cap on research-phase model turns (bounds cost).")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--model", default=MODEL)
    p.add_argument("--out", type=Path, help="Output directory (default: reviews/<date>-<topic>).")
    p.add_argument("--resume", action="store_true",
                   help="Continue from the state.json in --out instead of starting over.")
    p.add_argument("--synthesis-only", action="store_true",
                   help="Skip searching; write the review from the evidence already in --out.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = args.out or Path("reviews") / f"{date.today():%Y-%m-%d}-{slugify(args.topic)}"
    cfg = ReviewConfig(topic=args.topic, out_dir=out, criteria=args.criteria,
                       language=args.language, year_from=args.year_from,
                       year_to=args.year_to, target_papers=args.papers,
                       max_iterations=args.max_iterations, model=args.model,
                       effort=args.effort)

    if (args.resume or args.synthesis_only) and (out / "state.json").exists():
        lib = Library.load(out)
        print(f"Resuming {out} — {lib.counts()}")
    elif args.resume or args.synthesis_only:
        print(f"No state.json in {out} to resume from.", file=sys.stderr)
        return 2
    elif (out / "state.json").exists():
        print(f"{out} already holds a review. Use --resume, or choose another --out.",
              file=sys.stderr)
        return 2
    else:
        lib = Library(out, topic=args.topic)
        lib.save()

    client = anthropic.Anthropic()
    summary_path = out / "research_summary.md"
    try:
        if args.synthesis_only:
            summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        else:
            print(f"Phase 1 — research ({cfg.model}, effort {cfg.effort}) → {out}")
            summary = run_research(client, cfg, lib, HttpClient())
            summary_path.write_text(summary, encoding="utf-8")

        if not lib.with_status("included"):
            print("No papers were included, so there is nothing to synthesise.", file=sys.stderr)
            return 1
        print(f"\nPhase 2 — writing the review from {len(lib.with_status('included'))} papers…")
        review = run_synthesis(client, cfg, lib, summary)
    except anthropic.AuthenticationError:
        print("Claude API authentication failed: set ANTHROPIC_API_KEY.", file=sys.stderr)
        return 2
    except anthropic.APIError as exc:
        print(f"Claude API error: {exc}. Progress is saved; re-run with --resume.",
              file=sys.stderr)
        return 1

    report = finalize(review, lib)
    print("\nDone.")
    print(json.dumps(report, indent=1))
    print(f"Review:     {out / 'review.md'}\nReferences: {out / 'references.bib'}\n"
          f"All papers: {out / 'papers.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
