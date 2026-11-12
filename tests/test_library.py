import pytest

from scireview.agent import check_citations, finalize
from scireview.library import Library
from scireview.models import Paper


def make(title, author="Jean Dupont", year=2020, **kw):
    return Paper(title=title, authors=[author], year=year, **kw)


def test_keys_are_unique_and_readable(tmp_path):
    lib = Library(tmp_path)
    k1, _ = lib.add(make("A study"))
    k2, _ = lib.add(make("Another study"))
    k3, _ = lib.add(Paper(title="Untitled work"))
    assert (k1, k2, k3) == ("Dupont2020", "Dupont2020a", "Untitlednd")


def test_same_work_from_two_sources_is_merged(tmp_path):
    lib = Library(tmp_path)
    k1, new1 = lib.add(make("Water retention of laterite", sources=["arxiv"]))
    k2, new2 = lib.add(make("Water Retention of Laterite!", doi="10.1/X", abstract="long abstract",
                            citations=7, sources=["openalex"]))
    k3, new3 = lib.add(make("Different title variant", doi="https://doi.org/10.1/x"))
    assert new1 and not new2 and not new3 and k1 == k2 == k3
    p = lib.get(k1).paper
    assert p.doi == "10.1/X" and p.abstract == "long abstract" and p.citations == 7
    assert p.sources == ["arxiv", "openalex"]


def test_screening_evidence_and_reload(tmp_path):
    lib = Library(tmp_path, topic="t")
    key, _ = lib.add(make("A study"))
    with pytest.raises(ValueError):
        lib.add_evidence(key, {"key_results": "x"})  # must be included first
    lib.screen(key, "included", "on topic")
    lib.add_evidence(key, {"key_results": "k = 1e-20 m2"})
    lib.log_search("openalex", "q", 1, 1)
    lib.save()

    again = Library.load(tmp_path)
    assert again.counts() == {"candidate": 0, "included": 1, "excluded": 0, "total": 1}
    assert again.get(key).evidence[0]["key_results"] == "k = 1e-20 m2"
    assert again.add(make("A study"))[1] is False  # dedup index restored


def test_citations_are_checked_and_references_come_from_metadata(tmp_path):
    lib = Library(tmp_path)
    a, _ = lib.add(make("Paper A", author="Ana Silva", doi="10.5/a", venue="J. Soils"))
    b, _ = lib.add(make("Paper B", author="Bo Li"))
    c, _ = lib.add(make("Paper C", author="Cy Ng"))
    lib.screen(a, "included", "r")
    lib.screen(b, "included", "r")
    lib.screen(c, "excluded", "r")
    review = f"Claim [@{a}]. Both [@{b}; @{a}]. Excluded [@{c}]. Invented [@Fake1999]."
    cited, unknown = check_citations(review, lib)
    assert cited == [a, b, c, "Fake1999"] and unknown == [c, "Fake1999"]

    report = finalize(review, lib)
    text = (tmp_path / "review.md").read_text()
    assert "https://doi.org/10.5/a" in text and "Fake1999" in text.split("Citation check")[1]
    assert report["cited"] == 2 and report["uncited_included"] == []
    bib = (tmp_path / "references.bib").read_text()
    assert "@article{Silva2020" in bib and "Ng2020" not in bib
    assert (tmp_path / "papers.csv").read_text().count("\n") == 4
