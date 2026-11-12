"""The hosted runner: config files, kickoff event and the event-following logic."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from scireview import hosted
from scireview.hosted import EventHandler, load_ids, outcome_event

ROOT = Path(__file__).resolve().parents[1]


def frontmatter(path: Path) -> tuple[dict, str]:
    _, head, body = path.read_text().split("---", 2)
    return yaml.safe_load(head), body.strip()


def test_agent_file_is_valid_and_consistent():
    cfg, system = frontmatter(ROOT / hosted.AGENT_FILE)
    assert cfg["model"]["id"] == "claude-opus-5-5"
    toolset = cfg["tools"][0]
    assert toolset["type"] == "agent_toolset_20260401"
    assert toolset["default_config"]["permission_policy"]["type"] == "auto"
    assert {c["name"]: c["enabled"] for c in toolset["configs"]} == {
        "web_fetch": False, "web_search": False}
    assert cfg["multiagent"]["workflows"]["type"] == "enabled"
    # the prompt must say when to start a workflow run, and point at the mounted repo
    assert "workflow run" in system and hosted.REPO_MOUNT in system
    assert "--export /mnt/session/outputs" in system


def test_environment_allows_the_literature_databases():
    env = yaml.safe_load((ROOT / hosted.ENV_FILE).read_text())
    net = env["config"]["networking"]
    assert net["type"] == "limited" and net["allow_package_managers"] is True
    for host in ("api.openalex.org", "api.semanticscholar.org", "api.crossref.org",
                 "export.arxiv.org"):
        assert host in net["allowed_hosts"]


def test_every_command_the_prompt_names_exists():
    import re
    from scireview.tools import _parser
    _, system = frontmatter(ROOT / hosted.AGENT_FILE)
    commands = set(_parser()._subparsers._group_actions[0].choices)
    named = set(re.findall(r"`(\w+)", system)) - {"pip", "python", "scireview"}
    assert named and named <= commands, named - commands


def test_load_ids_from_lock_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SCIREVIEW_AGENT_ID", raising=False)
    monkeypatch.delenv("SCIREVIEW_ENV_ID", raising=False)
    lock = tmp_path / "claude-lock.json"
    lock.write_text(json.dumps({"resources": {
        "./agents/scientific-reviewer.md": {"id": "agent_1"},
        "./environments/scireview.yaml": {"id": "env_1"}}}))
    assert load_ids(lock) == ("agent_1", "env_1")
    with pytest.raises(SystemExit):
        load_ids(tmp_path / "missing.json")


def test_outcome_event_has_rubric_and_task():
    args = argparse.Namespace(topic="Lateritic soils", criteria="experimental", language="French",
                              year_from=2000, year_to=None, papers=24, max_revisions=4)
    ev = outcome_event(args)
    assert ev["type"] == "user.define_outcome" and ev["max_iterations"] == 4
    assert "French" in ev["description"] and "2000 to present" in ev["description"]
    assert "At least 16 papers are included" in ev["rubric"]["content"]


def ev(i, type, **kw):
    return NS(id=f"sevt_{i}", type=type, **kw)


def run(events, approve=lambda e: False):
    sent = []
    h = EventHandler(send=sent.append, approve=approve, log=lambda *_: None)
    stopped_at = None
    for i, e in enumerate(events):
        if h.handle(e):
            stopped_at = i
            break
    return h, sent, stopped_at


def test_waits_for_open_workflow_runs_before_finishing():
    events = [
        ev(1, "workflow_run.created", workflow_run_id="wrun_1", name="read", phases=[]),
        ev(2, "session.status_idle", stop_reason=NS(type="end_turn")),   # run still open
        ev(3, "workflow_run.status_ended", workflow_run_id="wrun_1", result=NS(type="completed")),
        ev(4, "span.outcome_evaluation_end", result="satisfied", iteration=0, explanation="ok"),
        ev(5, "session.status_idle", stop_reason=NS(type="end_turn")),
    ]
    h, _, stopped_at = run(events)
    assert stopped_at == 4 and h.finished == "done" and h.outcome == "satisfied"


def test_paused_tool_calls_are_answered_and_duplicates_ignored():
    call = ev(1, "agent.tool_use", name="bash", input={"command": "rm -rf /"},
              evaluated_permission="ask")
    events = [call, call, ev(2, "session.status_idle", stop_reason=NS(type="requires_action")),
              ev(3, "session.status_idle", stop_reason=NS(type="budget_reached"))]
    h, sent, stopped_at = run(events)
    assert sent == [[{"type": "user.tool_confirmation", "tool_use_id": "sevt_1",
                      "result": "deny"}]]
    assert stopped_at == 3 and h.finished == "budget"
