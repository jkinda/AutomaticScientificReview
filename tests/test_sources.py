from scireview.sources import parse_arxiv, parse_crossref, parse_openalex, parse_semantic_scholar


def test_parse_openalex_rebuilds_abstract_and_oa_pdf():
    data = {"results": [{
        "display_name": "Water retention of lateritic soils",
        "publication_year": 2019,
        "doi": "https://doi.org/10.1000/abc",
        "cited_by_count": 12,
        "abstract_inverted_index": {"Lateritic": [0], "soils": [1], "retain": [2], "water": [3]},
        "authorships": [{"author": {"display_name": "A. Diallo"}}],
        "primary_location": {"source": {"display_name": "Geoderma"}},
        "best_oa_location": {"pdf_url": "https://example.org/p.pdf"},
        "locations": [{"landing_page_url": "https://arxiv.org/abs/1810.06463v1"}],
    }, {"display_name": None}]}
    [p] = parse_openalex(data)
    assert p.abstract == "Lateritic soils retain water"
    assert p.pdf_url == "https://example.org/p.pdf"
    assert p.venue == "Geoderma" and p.year == 2019 and p.citations == 12
    assert p.arxiv_id == "1810.06463v1"


def test_parse_semantic_scholar():
    data = {"data": [{"title": "Permeability of cement paste", "year": 2018,
                      "authors": [{"name": "Z. Zhang"}], "abstract": None,
                      "externalIds": {"DOI": "10.1/x", "ArXiv": "1810.06463"},
                      "openAccessPdf": {"url": ""}, "citationCount": 5}]}
    [p] = parse_semantic_scholar(data)
    assert p.doi == "10.1/x" and p.arxiv_id == "1810.06463"
    assert p.pdf_url is None and p.abstract == ""


def test_parse_crossref_strips_jats():
    data = {"message": {"items": [{
        "title": ["Suction in <i>laterite</i>"],
        "abstract": "<jats:p>Results &amp; discussion</jats:p>",
        "author": [{"given": "B.", "family": "Kaboré"}],
        "issued": {"date-parts": [[2021, 3]]},
        "container-title": ["Eng. Geol."], "DOI": "10.2/y",
        "link": [{"URL": "https://x/y.pdf", "content-type": "application/pdf"}],
    }]}}
    [p] = parse_crossref(data)
    assert p.title == "Suction in laterite"
    assert p.abstract == "Results & discussion"
    assert p.year == 2021 and p.authors == ["B. Kaboré"] and p.pdf_url == "https://x/y.pdf"


ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1810.06463v1</id>
    <published>2018-09-04T00:00:00Z</published>
    <title>Determination of water permeability
      for cementitious materials</title>
    <summary>  Values of water permeability ... </summary>
    <author><name>Zhidong Zhang</name></author>
    <author><name>George Scherer</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/1810.06463v1" rel="related"/>
  </entry>
</feed>"""


def test_parse_arxiv():
    [p] = parse_arxiv(ARXIV_XML)
    assert p.title == "Determination of water permeability for cementitious materials"
    assert p.arxiv_id == "1810.06463v1" and p.year == 2018
    assert p.authors == ["Zhidong Zhang", "George Scherer"]
    assert p.pdf_url.endswith("1810.06463v1")
