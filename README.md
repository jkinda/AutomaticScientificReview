# AutomaticScientificReview

An agent that searches the scientific literature on a topic, screens what it finds, reads
the open-access papers and writes a cited review. It runs on Claude (Anthropic API) and
open scholarly databases: OpenAlex, Semantic Scholar, Crossref and arXiv.

## How it works

```
topic + criteria
      │
      ▼
Phase 1 — research (Claude tool-use loop)
  search_literature → screen_paper → read_full_text → record_evidence
      │   every decision is saved to reviews/<run>/state.json
      ▼
Phase 2 — synthesis (one streaming Claude call)
  evidence dossier → review in Markdown with [@Key] citations
      │
      ▼
Checks: every citation must be an included paper; the reference list is
built from database metadata, never from model output
      │
      ▼
review.md · references.bib · papers.csv · state.json · pdfs/ · fulltext/
```

| Tool the agent uses | What it does |
|---|---|
| `search_literature` | Queries one database; results are de-duplicated across sources by DOI, arXiv ID or title |
| `get_paper` | Full metadata and abstract of one paper |
| `screen_paper` | Include or exclude a paper, with a reason (kept for the audit trail) |
| `read_full_text` | Downloads the open-access PDF (rejecting paywall/HTML pages), extracts text, and returns it in pages |
| `record_evidence` | Stores study summary, methods, quantitative results and limitations: the material the review is written from |
| `list_library` | Shows progress |

## Install

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

Set your Anthropic API key (from https://console.anthropic.com):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Optional:

- `S2_API_KEY` — a free Semantic Scholar API key gives higher rate limits.
- `SCIREVIEW_CONTACT_EMAIL` — sent to OpenAlex and Crossref so they put you in their faster "polite pool".

## Use

```bash
scireview "Caractérisation des propriétés hydriques des sols latéritiques utilisés en construction" \
  --language French \
  --criteria "Experimental studies on water retention, permeability or capillarity of lateritic soils or laterite-based building materials" \
  --year-from 2000 --papers 25
```

The agent searches in English and in the topic's own language. Output goes to
`reviews/<date>-<topic>/`.

| Option | Meaning |
|---|---|
| `--criteria` | Inclusion criteria in plain words; the screening follows them |
| `--language` | Language of the written review (searching is always multilingual) |
| `--year-from`, `--year-to` | Publication-year window |
| `--papers` | Target number of included papers (default 25) |
| `--max-iterations` | Cap on research turns; this is the main cost control (default 80) |
| `--effort` | Claude effort level: `low` … `max` (default `high`) |
| `--resume` | Continue an interrupted run from its `state.json` |
| `--synthesis-only` | Re-write the review from evidence already collected |

For a first, cheaper trial run use `--papers 10 --max-iterations 30`.

## What to check in the output

- `review.md` ends with a **Citation check** note if the model cited anything outside the
  included papers. Verify those sentences.
- `papers.csv` lists every paper seen, with the include/exclude decision and reason. Use it
  to check the screening.
- Evidence marked `based_on: abstract` (in `state.json`) came from the abstract only,
  usually because no open-access PDF was available. Paywalled papers you can access
  yourself are worth reading before you rely on those claims.

This is a drafting and search aid. Results still need checking by someone who knows the
field before they are published.

## Hosted version (Claude Managed Agents)

The same review can run on Anthropic's infrastructure instead of your machine. Anthropic
runs the agent loop and a sandbox. This repository is mounted in the sandbox, and the
agent calls the same tools from its shell (`python -m scireview.tools ...`). You can close
your laptop while it works and come back to the results.

What the hosted version does differently:

- **Parallel reading.** When many papers need reading, the agent starts a *workflow
  run*: one sub-agent reads and extracts each paper, then a second agent checks every
  paper's numbers against the full text. Every sub-agent uses tokens, so each session
  has a hard spending cap (`--budget-usd`, default 30). To turn parallel reading off, set
  `workflows: {type: disabled}` in `agents/scientific-reviewer.md` and run `ant apply` again.
- **Graded until it passes.** The task is sent with a rubric (`RUBRIC` in
  `scireview/hosted.py`). A separate grader checks the review against it and sends it
  back for revision, up to `--max-revisions` times.
- **Restricted network.** The sandbox can reach PyPI, the four databases and a list of
  open-access hosts (`environments/scireview.yaml`). PDFs hosted anywhere else fall back
  to the abstract.

Managed Agents is in beta.

**One-time setup.** Install the [`ant` CLI](https://github.com/anthropics/anthropic-cli)
(1.30 or later) and log in with `ant auth login`. Then, from the repository root:

```bash
ant apply --dry-run -v agents/scientific-reviewer.md environments/scireview.yaml
```

```bash
ant apply agents/scientific-reviewer.md environments/scireview.yaml
```

This creates the agent and its environment and writes their IDs to `claude-lock.json`;
commit that file. After editing either file, run `ant apply` again to update them.

**Run a review.** You need a GitHub token that can read this repository (fine-grained,
*Contents: Read*). It is used to clone the repository and never enters the sandbox.

```bash
export GITHUB_TOKEN=github_pat_...
```

```bash
scireview-hosted "Caractérisation des propriétés hydriques des sols latéritiques utilisés en construction" \
  --language French --criteria "Experimental studies on water retention, permeability or capillarity of lateritic soils" \
  --papers 25 --budget-usd 30
```

The script prints a Console link where you can watch the session live, streams
progress, and saves the outputs to `reviews/<date>-<topic>-hosted/`. If the connection
drops or the budget is reached, re-attach with `--session <session id>`. Use `--branch` to
mount a branch other than `main`.

## Development

```bash
pytest
```

The tests run offline. They use saved API responses and a fake Claude API, so they need
no network access and no API key.

`notebooks/legacy_arxiv_scraper.ipynb` is the original arXiv-only prototype, kept for
reference.
