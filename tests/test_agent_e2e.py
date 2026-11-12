"""End-to-end run of both phases against a fake Claude API (no network, no key).

This checks that the requests the agent builds are accepted by the SDK, that the
tool loop drives the library correctly, and that the outputs are written.
"""

import json

import anthropic
import httpx2

from scireview import agent, sources
from scireview.agent import ReviewConfig, finalize, run_research, run_synthesis
from scireview.library import Library
from scireview.models import Paper
from scireview.toolbox import ToolBox


def msg(content, stop_reason):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": agent.MODEL,
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def tool_use(i, name, **inp):
    return {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}


SCRIPT = [
    msg([tool_use(1, "search_literature", query="laterite water retention", source="openalex")],
        "tool_use"),
    msg([tool_use(2, "screen_paper", key="Diallo2019", decision="included", reason="On topic"),
         tool_use(3, "screen_paper", key="Smith2020", decision="excluded", reason="Off topic"),
         tool_use(4, "record_evidence", key="Diallo2019", study_summary="Laterite from Senegal",
                  methods="Pressure plate", key_results="AEV 20 kPa", limitations="1 site",
                  based_on="abstract")], "tool_use"),
    msg([{"type": "text", "text": "Search done; one relevant study."}], "end_turn"),
]

REVIEW = "# Review\n\nAir-entry value of about 20 kPa [@Diallo2019]."


def sse(text):
    events = [
        ("message_start", {"type": "message_start", "message": msg([], None)}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


def test_full_run_with_fake_api(tmp_path, monkeypatch):
    monkeypatch.setitem(sources.SEARCHERS, "openalex", lambda *a: [
        Paper(title="Water retention of laterite", authors=["Awa Diallo"], year=2019,
              abstract="We measured...", doi="10.1/a"),
        Paper(title="Unrelated", authors=["J. Smith"], year=2020),
    ])
    requests_seen = []
    script = iter(SCRIPT)

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests_seen.append((dict(request.headers), body))
        if body.get("stream"):
            return httpx2.Response(200, text=sse(REVIEW),
                                   headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, json=next(script))

    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(
                                     transport=httpx2.MockTransport(handler)))
    cfg = ReviewConfig(topic="Water retention of lateritic soils", out_dir=tmp_path)
    box = ToolBox(tmp_path, topic=cfg.topic, log=lambda *_: None)

    summary = run_research(client, cfg, box, log=lambda *_: None)
    lib = Library.load(tmp_path)
    assert summary == "Search done; one relevant study."
    assert lib.counts() == {"candidate": 0, "included": 1, "excluded": 1, "total": 2}
    assert lib.searches[0]["new"] == 2

    review = run_synthesis(client, cfg, lib, summary)
    report = finalize(review, lib)
    assert report["cited"] == 1 and report["unknown_citations"] == []
    written = (tmp_path / "review.md").read_text()
    assert "- **[Diallo2019]** Awa Diallo (2019). Water retention of laterite." in written
    assert "Citation check" not in written

    headers, first = requests_seen[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert {t["name"] for t in first["tools"]} >= {"search_literature", "record_evidence"}
    # tool results from the second turn were sent back in a single user message
    _, third = requests_seen[2]
    assert [b["type"] for b in third["messages"][-1]["content"]] == ["tool_result"] * 3
    _, synth = requests_seen[-1]
    assert "Diallo2019" in synth["messages"][0]["content"] and "Smith2020" not in \
        synth["messages"][0]["content"].split("<evidence_dossier>")[1]
