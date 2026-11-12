"""Command-line access to the review tools, for agents that work through a shell.

The hosted Managed Agent runs these from its sandbox, e.g.::

    python -m scireview.tools init --topic "..." --year-from 2000
    python -m scireview.tools search "laterite water retention" --source openalex
    python -m scireview.tools screen Diallo2019 included "Measures SWRC of laterite"
    python -m scireview.tools read Diallo2019 --start 15000
    python -m scireview.tools evidence Diallo2019 --summary ... --methods ... \\
        --results ... --limitations ... --based-on full_text
    python -m scireview.tools list --status included
    python -m scireview.tools dossier > dossier.json
    python -m scireview.tools finalize draft.md --export /mnt/session/outputs

Every command prints one JSON object. Errors print {"error": ...} and exit 1.
The review directory is --dir, or $SCIREVIEW_DIR, or ./review.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import check_citations, finalize
from .sources import SOURCES
from .toolbox import ToolBox, ToolError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scireview.tools",
                                description="Review tools for shell-based agents.")
    p.add_argument("--dir", type=Path,
                   default=Path(os.environ.get("SCIREVIEW_DIR", "review")),
                   help="Review directory holding state.json (default $SCIREVIEW_DIR or ./review)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Create or update the review's settings")
    s.add_argument("--topic", required=True)
    s.add_argument("--criteria", default="")
    s.add_argument("--language", default="")
    s.add_argument("--year-from", type=int)
    s.add_argument("--year-to", type=int)

    s = sub.add_parser("search", help="Search a database and add results to the library")
    s.add_argument("query")
    s.add_argument("--source", default="openalex", choices=SOURCES)
    s.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("paper", help="Full metadata, abstract and evidence of one paper")
    s.add_argument("key")

    s = sub.add_parser("screen", help="Include or exclude a paper")
    s.add_argument("key")
    s.add_argument("decision", choices=["included", "excluded"])
    s.add_argument("reason")

    s = sub.add_parser("read", help="Read the open-access full text, ~15,000 chars at a time")
    s.add_argument("key")
    s.add_argument("--start", type=int, default=0)

    s = sub.add_parser("evidence", help="Record evidence for an included paper")
    s.add_argument("key")
    s.add_argument("--summary", required=True, help="What was studied")
    s.add_argument("--methods", required=True)
    s.add_argument("--results", required=True, help="Key results with numbers and units")
    s.add_argument("--limitations", required=True)
    s.add_argument("--based-on", default="full_text", choices=["full_text", "abstract"])
    s.add_argument("--replace", action="store_true",
                   help="Replace this paper's earlier evidence (to correct it)")

    s = sub.add_parser("list", help="List papers by status")
    s.add_argument("--status", default="included",
                   choices=["candidate", "included", "excluded"])

    sub.add_parser("dossier", help="Settings, search log, counts and evidence of included papers")

    s = sub.add_parser("check", help="Check the citations in a draft without writing outputs")
    s.add_argument("draft", type=Path)

    s = sub.add_parser("finalize", help="Check citations, add references, write all outputs")
    s.add_argument("draft", type=Path, help="Markdown review citing papers as [@Key]")
    s.add_argument("--export", type=Path, help="Also copy the outputs to this directory")
    return p


def run(argv: list[str] | None = None) -> dict:
    args = _parser().parse_args(argv)
    log = lambda msg: print(msg, file=sys.stderr)  # noqa: E731 - progress to stderr, JSON to stdout
    box = ToolBox(args.dir, log=log)

    if args.cmd == "init":
        with box.library() as lib:
            lib.topic = args.topic
            lib.settings.update({k: v for k, v in {
                "criteria": args.criteria, "language": args.language,
                "year_from": args.year_from, "year_to": args.year_to}.items() if v})
            return {"ok": True, "topic": lib.topic, "settings": lib.settings}
    if args.cmd == "search":
        return box.search(args.query, args.source, args.limit)
    if args.cmd == "paper":
        return box.get_paper(args.key)
    if args.cmd == "screen":
        return box.screen(args.key, args.decision, args.reason)
    if args.cmd == "read":
        return box.read(args.key, args.start)
    if args.cmd == "evidence":
        return box.record_evidence(args.key, args.summary, args.methods, args.results,
                                   args.limitations, args.based_on, args.replace)
    if args.cmd == "list":
        return box.list(args.status)
    if args.cmd == "dossier":
        with box.library(write=False) as lib:
            return {"topic": lib.topic, "settings": lib.settings, "counts": lib.counts(),
                    "searches": lib.searches, "included": lib.evidence_dossier()}
    if args.cmd in ("check", "finalize"):
        if not args.draft.exists():
            raise ToolError(f"{args.draft} does not exist")
        draft = args.draft.read_text(encoding="utf-8")
        with box.library(write=args.cmd == "finalize") as lib:
            if args.cmd == "check":
                cited, unknown = check_citations(draft, lib)
                included = [e.paper.key for e in lib.with_status("included")]
                return {"cited": cited, "unknown_citations": unknown,
                        "uncited_included": [k for k in included if k not in cited]}
            return finalize(draft, lib, export_dir=args.export)
    raise ToolError(f"unknown command {args.cmd}")


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(argv)
    except ToolError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
