"""Run a review on the hosted Scientific Reviewer agent (Claude Managed Agents).

One-time setup creates the agent and its sandbox from the files in agents/ and
environments/ (see the README). Each run of this script then:

1. starts a session with this repository mounted in the sandbox and a spending cap;
2. kicks it off with an *outcome*: the review task plus a rubric that a separate
   grader checks; the agent revises until the rubric is met or runs out of tries;
3. streams progress (searches, workflow runs reading papers in parallel, grading);
4. downloads review.md, references.bib, papers.csv and state.json.

    scireview-hosted "topic" --criteria "..." --language French --budget-usd 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

import anthropic

from .cli import slugify

AGENT_FILE = "agents/scientific-reviewer.md"
ENV_FILE = "environments/scireview.yaml"
REPO_URL = "https://github.com/jkinda/AutomaticScientificReview"
REPO_MOUNT = "/workspace/AutomaticScientificReview"
MA_BETA = "managed-agents-2026-04-01"
CONSOLE = "https://platform.claude.com/workspaces/default/sessions/{}"

# Starter rubric - tune the criteria to what a good review means in your field.
RUBRIC = """\
# Literature review rubric (starter - tune it)

1. /mnt/session/outputs contains review.md, references.bib, papers.csv and state.json,
   produced by `python -m scireview.tools finalize`.
2. review.md is written in {language} and has these sections: Abstract, Introduction,
   Methodology of this review, at least two thematic sections, Discussion, and
   Conclusions and research directions.
3. The Methodology section names the databases and representative queries used, and
   its screening counts (papers seen, included, excluded) match papers.csv.
4. At least {min_papers} papers are included, each with an inclusion reason that follows
   the criteria ({criteria}) and at least one evidence record in state.json.
5. review.md has no "Citation check" warning: every [@Key] citation is an included paper.
6. Every included paper is cited at least once, and every factual statement about the
   literature carries a citation.
7. Every quantitative result in review.md (with its units and test conditions) matches
   the evidence recorded for the cited paper in state.json.
8. The review compares studies by theme, method or material rather than summarising
   them one by one, and points out where results agree or conflict.
9. The Discussion names at least three concrete research gaps or contradictions, each
   tied to cited studies.
10. No placeholder text, TODOs or empty sections remain.
"""


# ----------------------------------------------------------------------------- setup

def load_ids(lock_path: Path = Path("claude-lock.json")) -> tuple[str, str]:
    """Agent and environment IDs from the environment, or from ant apply's lock file."""
    agent_id = os.environ.get("SCIREVIEW_AGENT_ID")
    env_id = os.environ.get("SCIREVIEW_ENV_ID")
    if (not agent_id or not env_id) and lock_path.exists():
        resources = json.loads(lock_path.read_text()).get("resources", {})

        def find(path: str) -> str | None:
            for key in (f"./{path}", path):
                if key in resources:
                    return resources[key].get("id")
            return None

        agent_id = agent_id or find(AGENT_FILE)
        env_id = env_id or find(ENV_FILE)
    if not agent_id or not env_id:
        raise SystemExit(
            "Agent or environment ID not found. Run the one-time setup first:\n"
            f"  ant apply {AGENT_FILE} {ENV_FILE}\n"
            "from the repository root (it writes claude-lock.json), or set "
            "SCIREVIEW_AGENT_ID and SCIREVIEW_ENV_ID.")
    return agent_id, env_id


def outcome_event(args: argparse.Namespace) -> dict[str, Any]:
    years = ""
    if args.year_from or args.year_to:
        years = f" Publication years: {args.year_from or 'any'} to {args.year_to or 'present'}."
    criteria = args.criteria or "studies that directly address the topic"
    description = (
        f"Write a systematic literature review on: {args.topic}\n"
        f"Inclusion criteria: {criteria}.{years}\n"
        f"Write the review in {args.language}. Aim for about {args.papers} included papers "
        "with recorded evidence. Follow the method in your instructions, and finish by "
        "running finalize with --export /mnt/session/outputs.")
    rubric = RUBRIC.format(language=args.language, criteria=criteria,
                           min_papers=max(3, args.papers * 2 // 3))
    return {"type": "user.define_outcome", "description": description,
            "rubric": {"type": "text", "content": rubric},
            "max_iterations": args.max_revisions}


# --------------------------------------------------------------------------- events

def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        obj = getattr(obj, name, None) if not isinstance(obj, dict) else obj.get(name)
        if obj is None:
            return default
    return obj


@dataclass
class EventHandler:
    """Follows a session's events until the review is done (or cannot continue).

    ``send`` posts events back to the session; ``approve`` decides on tool calls the
    server paused for a human (it should deny when nobody is watching).
    """

    send: Callable[[list[dict[str, Any]]], None]
    approve: Callable[[Any], bool]
    log: Callable[[str], None] = print
    open_runs: set[str] = field(default_factory=set)
    seen: set[str] = field(default_factory=set)
    outcome: str | None = None
    finished: str | None = None  # why we stopped: done, budget, error, terminated

    def handle(self, event: Any) -> bool:
        """Process one event; return True when the session needs nothing more from us."""
        if event.id in self.seen:
            return self.finished is not None
        self.seen.add(event.id)
        t = event.type
        if t == "agent.message":
            text = "".join(b.text for b in event.content if getattr(b, "type", "") == "text")
            if text.strip():
                self.log(text.strip())
        elif t in ("agent.tool_use", "agent.mcp_tool_use") \
                and getattr(event, "evaluated_permission", None) == "ask":
            allow = self.approve(event)
            self.send([{"type": "user.tool_confirmation", "tool_use_id": event.id,
                        "result": "allow" if allow else "deny"}])
            self.log(f"[tool call {getattr(event, 'name', '?')} {'allowed' if allow else 'denied'}]")
        elif t == "workflow_run.created":
            self.open_runs.add(event.workflow_run_id)
            self.log(f"[workflow run started: {getattr(event, 'name', '')} - "
                     f"{len(getattr(event, 'phases', None) or [])} phases]")
        elif t == "workflow_run.status_ended":
            self.open_runs.discard(event.workflow_run_id)
            self.log(f"[workflow run ended: {_attr(event, 'result', 'type', default='?')}]")
        elif t == "workflow_run.error":
            self.log(f"[workflow run error: {_attr(event, 'error', 'message', default='?')}]")
        elif t == "span.outcome_evaluation_end":
            self.outcome = event.result
            self.log(f"[grader, round {event.iteration + 1}: {event.result}] "
                     f"{getattr(event, 'explanation', '')}")
        elif t == "session.error":
            self.log(f"[session error: {_attr(event, 'error', 'message', default=event)}]")
        elif t == "session.status_terminated":
            self.finished = "terminated"
        elif t == "session.status_idle":
            reason = _attr(event, "stop_reason", "type", default="")
            if reason == "end_turn" and not self.open_runs:
                self.finished = "done"
            elif reason == "budget_reached":
                self.finished = "budget"
            elif reason == "retries_exhausted":
                self.finished = "error"
            # requires_action: a paused call is waiting; it was answered above.
            # end_turn with runs open: the agent takes a turn when each run ends.
        return self.finished is not None


def follow(client: anthropic.Anthropic, session_id: str, handler: EventHandler,
           kickoff: list[dict[str, Any]] | None = None) -> None:
    """Stream events (sending ``kickoff`` once the stream is open), reconnecting on drops."""
    attempts = 0
    while True:
        try:
            with client.beta.sessions.events.stream(session_id=session_id) as stream:
                if kickoff:
                    client.beta.sessions.events.send(session_id=session_id, events=kickoff)
                    kickoff = None
                else:  # reconnect: replay what happened while we were away
                    for event in client.beta.sessions.events.list(session_id=session_id):
                        if handler.handle(event):
                            return
                for event in stream:
                    attempts = 0
                    if handler.handle(event):
                        return
            problem = "stream closed"  # closed before the session finished: reconnect
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            problem = f"stream dropped ({exc.__class__.__name__})"
        attempts += 1
        if attempts > 5:
            raise RuntimeError(f"Lost the event stream for session {session_id}; "
                               "re-attach later with --session.")
        handler.log(f"[{problem}; reconnecting]")
        time.sleep(min(2 ** attempts, 30))


def download_outputs(client: anthropic.Anthropic, session_id: str, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for _ in range(5):  # outputs are indexed a few seconds after the session goes idle
        files = list(client.beta.files.list(scope_id=session_id, betas=[MA_BETA]))
        if files:
            break
        time.sleep(3)
    saved = []
    for f in files:
        dest = out / Path(f.filename).name
        client.beta.files.download(f.id).write_to_file(dest)
        saved.append(dest)
    return saved


def interactive_approve(event: Any) -> bool:
    """Ask at the terminal; deny when nobody is there to answer."""
    if not sys.stdin.isatty():
        return False
    print(f"\nThe agent wants to run {getattr(event, 'name', '?')}:\n"
          f"{json.dumps(getattr(event, 'input', {}), ensure_ascii=False, indent=1)[:2000]}")
    return input("Allow? [y/N] ").strip().lower() == "y"


# ------------------------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="scireview-hosted",
                                description="Run a literature review on Claude Managed Agents.")
    p.add_argument("topic")
    p.add_argument("--criteria", default="")
    p.add_argument("--language", default="English")
    p.add_argument("--year-from", type=int)
    p.add_argument("--year-to", type=int)
    p.add_argument("--papers", type=int, default=25, help="Target number of included papers")
    p.add_argument("--budget-usd", type=float, default=30.0,
                   help="Hard cap on the session's spend at list prices (default 30)")
    p.add_argument("--max-revisions", type=int, default=3,
                   help="How many times the grader may send the review back (1-20)")
    p.add_argument("--branch", default=os.environ.get("SCIREVIEW_BRANCH", "main"),
                   help="Branch of this repository to mount in the sandbox")
    p.add_argument("--repo", default=os.environ.get("SCIREVIEW_REPO", REPO_URL))
    p.add_argument("--out", type=Path, help="Where to save the outputs")
    p.add_argument("--session", help="Re-attach to an existing session instead of starting one")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = args.out or Path("reviews") / f"{date.today():%Y-%m-%d}-{slugify(args.topic)}-hosted"
    client = anthropic.Anthropic()

    if args.session:
        session_id, kickoff = args.session, None
    else:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            print("Set GITHUB_TOKEN to a GitHub token that can read the repository "
                  "(fine-grained, Contents: Read). It never enters the sandbox.", file=sys.stderr)
            return 2
        agent_id, env_id = load_ids()
        session = client.beta.sessions.create(
            agent=agent_id,
            environment_id=env_id,
            title=f"Review: {args.topic[:200]}",
            resources=[{"type": "github_repository", "url": args.repo,
                        "authorization_token": token, "mount_path": REPO_MOUNT,
                        "checkout": {"type": "branch", "name": args.branch}}],
            budget={"type": "limit",
                    "max_list_cost": {"amount": str(round(args.budget_usd * 100)),
                                      "currency": "USD"}},
        )
        session_id, kickoff = session.id, [outcome_event(args)]
    print(f"Session {session_id}\nWatch it live: {CONSOLE.format(session_id)}\n")

    handler = EventHandler(
        send=lambda events: client.beta.sessions.events.send(session_id=session_id,
                                                             events=events),
        approve=interactive_approve)
    follow(client, session_id, handler, kickoff)

    if handler.finished == "budget":
        print(f"\nThe session reached its ${args.budget_usd:.0f} budget and is paused, with "
              "its work kept. Raise the budget in the Console (or with "
              "client.beta.sessions.update) and re-attach with --session " + session_id,
              file=sys.stderr)
    elif handler.finished in ("error", "terminated"):
        print(f"\nThe session stopped ({handler.finished}). See the Console for details. "
              f"To nudge it on, send it a message and re-attach with --session {session_id}.",
              file=sys.stderr)

    saved = download_outputs(client, session_id, out)
    print(f"\nGrader verdict: {handler.outcome or 'none yet'}")
    for path in saved:
        print(f"Saved {path}")
    if not saved:
        print("No output files yet.", file=sys.stderr)
    return 0 if handler.finished == "done" and saved else 1


if __name__ == "__main__":
    sys.exit(main())
