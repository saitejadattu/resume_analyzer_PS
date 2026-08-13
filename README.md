# Resume Shortlisting System

A production-ready, **pure-Python (no LLM)** pipeline that shortlists candidates
against a Job Description. It reads candidate data from a **Google Sheet** (or a
local Excel/CSV), downloads each resume PDF, extracts and parses the text, and
scores every candidate using **keyword matching across both the Skills and
Projects sections** — with Projects weighted higher, exactly as required.

Keyword shortlisting is the priority: a skill that appears only inside a project
description (e.g. *React* / *LangChain* inside an "AI chatbot" project) is still
counted, and descriptive phrases like *"retrieval augmented generation"* are
mapped to technologies (RAG, GenAI, LLM) via a deterministic, editable rule
engine — **no API keys, free to run on 1000+ resumes**.

---

## Features

| Step | Capability |
|------|-----------|
| 1 | Read candidates from a public Google Sheet or local Excel/CSV (configurable column mapping). |
| 2, 21 | Concurrent, cached, retrying resume downloads (`ThreadPoolExecutor`). Handles timeouts, 404s, invalid URLs, network failures — one failure never stops the run. |
| 3 | Clean text extraction with PyMuPDF, cached to `extracted_text/`. |
| 4 | Section parsing (Skills, Projects, Experience, Education, Certifications…). |
| 5 | JD parsing from `.txt` / `.pdf` / `.docx` / inline string → required + preferred skills. |
| 6 | **Keyword matching across Skills *and* Projects**, alias-aware, project-priority. |
| 7 | Per-project technology detection. |
| 8 | Rule-based technology **inference** from descriptive language (replaces the LLM). |
| 9, 10 | GitHub link discovery + validation → Working / Broken / Private / Not Found. |
| 11 | Structured project extraction (name, technologies, GitHub link, live link, description). |
| 12, 13 | Configurable weighted scoring (0–100) + recommendation bands. |
| 14, 15 | Excel + JSON reports. |
| 16–22 | Modular OOP architecture, config-driven, logging, CLI, pluggable sources. |

---

## Project structure

```
resume_shortlisting/
    config.py          # weights, timeouts, retries, folders, column mapping
    models.py          # Pydantic models (Candidate, ParsedResume, JDSpec, ScoreResult…)
    utils.py           # logging, hashing, caching paths, text cleaning
    main.py            # CLI entry point
    pipeline.py        # per-candidate processing + concurrent fan-out
    sources/           # pluggable data sources (Google Sheet, Excel) — Step 22
    downloader.py      # concurrent resume download w/ retry + cache
    text_extractor.py  # PyMuPDF PDF -> text
    parser.py          # resume section + project + GitHub parsing
    jd_parser.py       # JD -> required/preferred skills
    skills_kb.py       # loads data/skills.yaml, compiles matchers
    tech_detector.py   # rule-based per-project tech detection (no LLM)
    keyword_matcher.py # THE priority module: Skills + Projects matching
    github_checker.py  # GitHub link validation
    scorer.py          # weighted scoring + recommendation
    excel_writer.py    # outputs/final_shortlisted.xlsx
    json_writer.py     # outputs/report.json
    data/skills.yaml   # skill aliases + phrase inference rules (edit to extend!)
    resumes/           # cached downloaded PDFs
    extracted_text/    # cached extracted text
    outputs/           # final_shortlisted.xlsx + report.json
    logs/app.log       # per-resume processing log
```

---

## Setup

Requires **Python 3.12+** (tested on 3.14).

```bash
pip install -r requirements.txt
```

Dependencies: `pandas`, `openpyxl`, `requests`, `tenacity`, `PyMuPDF`,
`python-docx`, `PyYAML`, `pydantic`, `tqdm`.

---

## Usage

### Option A — Web UI (Streamlit) 🖥️

The easiest way: a local web app where you **type the required/preferred keywords**,
paste the sheet URL (or upload an Excel/CSV), and run — with a live progress bar,
a sortable results table, and one-click Excel/JSON downloads.

```bash
streamlit run app.py
```

Then open the URL it prints (usually http://localhost:8501). In the sidebar pick
your source; in the main panel select known skills from the auto-complete or type
custom keywords; click **Run shortlisting**.

### Option B — Command line

#### From a Google Sheet (must be shared "Anyone with the link can view")

```bash
python -m resume_shortlisting.main \
    --sheet "https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0" \
    --jd jd.pdf
```

### From a local Excel file

```bash
python -m resume_shortlisting.main --excel students.xlsx --jd jd.docx
```

#### With explicit keywords (no JD file — recommended for keyword shortlisting)

```bash
python -m resume_shortlisting.main --sheet "<url>" \
    --required "Python, Django, FastAPI, React, PostgreSQL, Docker" \
    --preferred "LangChain, OpenAI, AWS, Redis"
```

`--required` / `--preferred` take precedence over `--jd`. You can also still pass
an inline JD string via `--jd "Python, Django, React"`.

### Outputs

```
outputs/final_shortlisted.xlsx   # sorted by score, all Step-14 columns
outputs/report.json              # machine-readable report
logs/app.log                     # one line per resume processed
```

### Useful flags

| Flag | Purpose |
|------|---------|
| `--required "..."` | Comma-separated required keywords (overrides `--jd`). |
| `--preferred "..."` | Comma-separated preferred keywords. |
| `--no-github` | Skip GitHub validation (faster; avoids rate limits). |
| `--limit N`   | Process only the first N candidates (dry run). |
| `--excel-out` / `--json-out` | Override output paths. |
| `--verbose`   | Debug-level logging. |

---

## Configuration

Everything tunable lives in [`resume_shortlisting/config.py`](resume_shortlisting/config.py):

- **Scoring weights** (`ScoreWeights`) — e.g. required skill in Projects `+10`,
  in Skills `+5`, preferred `+3`, GitHub working `+15`, GenAI project `+20`,
  no GitHub `−15`, broken GitHub `−20`.
- **Recommendation bands** (`RecommendationBands`) — Strong Shortlist / Shortlist
  / Consider / Reject thresholds.
- **Column aliases** (`COLUMN_ALIASES`) — accepted header names per field, so
  sheets with slightly different headers work without code changes.
- **Timeouts, retry counts, worker counts** — for downloads and GitHub checks.
- **Section headings** recognised by the resume parser.

Secrets are read from the environment, never hard-coded:

- `GITHUB_TOKEN` (optional) — raises the GitHub API rate limit from 60/hr to
  5000/hr, recommended for 1000+ resumes.

### Extending the skill knowledge base

Teach the matcher new skills/aliases or inference rules by editing
[`resume_shortlisting/data/skills.yaml`](resume_shortlisting/data/skills.yaml) —
**no Python changes required**:

```yaml
canonical_skills:
  React: [react, reactjs, "react.js"]
phrase_rules:
  "retrieval augmented generation": [RAG, GenAI, LLM]
genai_markers: [GenAI, LLM, RAG, LangChain, OpenAI]
```

---

## How scoring works (example)

A candidate whose Skills list only says *Python, SQL, Git*, but whose projects
describe *"an AI chatbot using React and FastAPI with retrieval augmented
generation via LangChain and OpenAI"* and *"a hospital system using Django and
PostgreSQL"*:

- Matches **Python, React, Django, FastAPI, PostgreSQL, Docker** (required) —
  most evidenced in **Projects** (higher weight).
- Infers **RAG, GenAI, LLM** from the phrase → GenAI-project bonus.
- Two projects use required tech → multi-project bonus.
- **Score ≈ 94/100 → Strong Shortlist**, with a remark explaining exactly why.

---

## Design notes (future-ready — Step 22)

- **Pluggable sources**: add Google Drive / S3 / DB by subclassing
  `sources.base.ResumeSource` — the scoring/matching code never changes.
- **Independent modules**: `keyword_matcher` and `tech_detector` are decoupled,
  so either can be upgraded (fuzzy matching, embeddings, or a real LLM) behind
  the same interface without touching the rest of the pipeline.
- **Resilience**: every stage is exception-guarded; a failed resume yields a
  scored result with an explanatory remark instead of aborting the batch.
