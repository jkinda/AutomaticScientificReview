"""The shell tools the hosted agent uses, including several agents writing at once."""

import json
import multiprocessing as mp

from scireview import sources, tools
from scireview.library import Library
from scireview.models import Paper

FAKE = [Paper(title=f"Study {i}", authors=[f"Author{i} Name{i}"], year=2010 + i,
              abstract="water retention of laterite") for i in range(12)]


def cli(tmp_path, *argv, capsys):
    code = tools.main(["--dir", str(tmp_path), *argv])
    return code, json.loads(capsys.readouterr().out)


def test_full_cli_flow(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sources.SEARCHERS, "openalex", lambda http, q, n, yf, yt: FAKE[:3])
    code, out = cli(tmp_path, "init", "--topic", "Laterite", "--year-from", "2005", capsys=capsys)
    assert code == 0 and out["settings"] == {"year_from": 2005}

    code, out = cli(tmp_path, "search", "laterite water", capsys=capsys)
    keys = [r["key"] for r in out["results"]]
    assert code == 0 and len(keys) == 3 and out["library"]["candidate"] == 3

    code, out = cli(tmp_path, "evidence", keys[0], "--summary", "s", "--methods", "m",
                    "--results", "r", "--limitations", "l", capsys=capsys)
    assert code == 1 and "not included" in out["error"]

    cli(tmp_path, "screen", keys[0], "included", "on topic", capsys=capsys)
    cli(tmp_path, "screen", keys[1], "excluded", "off topic", capsys=capsys)
    for results in ("first", "corrected"):
        cli(tmp_path, "evidence", keys[0], "--summary", "s", "--methods", "m", "--results",
            results, "--limitations", "l", "--based-on", "abstract", "--replace", capsys=capsys)
    code, out = cli(tmp_path, "dossier", capsys=capsys)
    [paper] = out["included"]
    assert [e["key_results"] for e in paper["evidence"]] == ["corrected"]
    assert out["counts"]["excluded"] == 1 and out["searches"][0]["new"] == 3

    code, out = cli(tmp_path, "read", "Nope1999", capsys=capsys)
    assert code == 1 and "Unknown paper key" in out["error"]

    draft = tmp_path / "draft.md"
    draft.write_text(f"Finding [@{keys[0]}] and [@{keys[1]}].")
    code, out = cli(tmp_path, "check", str(draft), capsys=capsys)
    assert out["unknown_citations"] == [keys[1]]
    draft.write_text(f"Finding [@{keys[0]}].")
    export = tmp_path / "outputs"
    code, out = cli(tmp_path, "finalize", str(draft), "--export", str(export), capsys=capsys)
    assert code == 0 and out["cited"] == 1
    assert sorted(p.name for p in export.iterdir()) == [
        "papers.csv", "references.bib", "review.md", "state.json"]


def _screen_many(args):
    directory, keys, worker = args
    for key in keys:
        tools.main(["--dir", directory, "screen", key, "included", f"worker {worker}"])
        tools.main(["--dir", directory, "evidence", key, "--summary", "s", "--methods", "m",
                    "--results", f"from worker {worker}", "--limitations", "l"])


def test_parallel_agents_do_not_lose_each_others_work(tmp_path, capsys):
    lib = Library(tmp_path, topic="t")
    keys = [lib.add(p)[0] for p in FAKE]
    lib.save()
    chunks = [(str(tmp_path), keys[i::4], i) for i in range(4)]
    with mp.get_context("fork").Pool(4) as pool:
        pool.map(_screen_many, chunks)
    capsys.readouterr()
    final = Library.load(tmp_path)
    assert final.counts()["included"] == 12
    assert all(len(final.get(k).evidence) == 1 for k in keys)
