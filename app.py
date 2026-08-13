"""Streamlit UI for the Resume Shortlisting System.

Run with:
    streamlit run app.py

Lets you paste a Google Sheet URL (or upload an Excel/CSV), type the required and
preferred keywords to search for in resumes, run the shortlister with a live
progress bar, browse a sortable results table, and download the Excel/JSON.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from resume_shortlisting import config
from resume_shortlisting.core import (
    build_jd_from_keywords,
    parse_keyword_string,
    run_shortlisting,
)
from resume_shortlisting.excel_writer import to_dataframe
from resume_shortlisting.models import GithubStatus, JDSpec, ScoreResult
from resume_shortlisting.skills_kb import load_kb
from resume_shortlisting.sources import ExcelSource, GoogleSheetSource
from resume_shortlisting.profile_exports import profile_name, profile_path, profiles_zip
from resume_shortlisting.utils import setup_logging, url_hash

st.set_page_config(page_title="Resume Shortlisting", page_icon="📄", layout="wide")

setup_logging(to_console=False)
config.ensure_dirs()


@st.cache_resource(show_spinner=False)
def _kb():
    """Load the skills knowledge base once per server session."""
    return load_kb()


def _known_skill_names() -> list[str]:
    return sorted(_kb().canonical_skills.keys())


def _candidate_widget_id(result: ScoreResult, occurrence: int = 0) -> str:
    """Stable page-local ID that remains safe when names or URLs repeat.

    Resume URL is the existing primary file identifier. Name/email distinguish
    separate rows sharing a URL; the occurrence suffix handles exact duplicate
    rows without changing their visible labels.
    """
    identity = "\0".join((result.candidate.resume_url, result.candidate.name, result.candidate.email))
    return f"{url_hash(identity)}_{occurrence}"


# --------------------------------------------------------------------------- #
# Visual helpers — where each keyword matched + GitHub status
# --------------------------------------------------------------------------- #
_GH_MATRIX_CELL = {
    GithubStatus.WORKING: "✅ Working",
    GithubStatus.BROKEN: "❌ Broken",
    GithubStatus.NOT_FOUND: "⚠️ Not found",
    GithubStatus.PRIVATE: "🔒 Private",
    GithubStatus.NONE: "🚫 None",
}
_GH_EMOJI = {
    GithubStatus.WORKING: "✅",
    GithubStatus.BROKEN: "❌",
    GithubStatus.NOT_FOUND: "⚠️",
    GithubStatus.PRIVATE: "🔒",
    GithubStatus.NONE: "🚫",
}


def _keyword_cell(result: ScoreResult, keyword: str) -> str:
    """Matrix cell: where a keyword matched for one candidate."""
    sections = result.matched_in.get(keyword, [])
    if "Projects" in sections:
        return "✅ Project"
    if "Skills" in sections:
        return "🟡 Skills"
    return "—"


def _github_cell(result: ScoreResult) -> str:
    """Matrix cell: the candidate's GitHub headline status."""
    if not result.github_found:
        return "🚫 None"
    if not result.github_checked:
        return "⏸️ Not checked"
    return _GH_MATRIX_CELL.get(result.github_status, "❓")


def _keyword_matrix(results: list[ScoreResult], jd: JDSpec) -> pd.DataFrame:
    """Build the candidate × keyword grid (+ Score and GitHub columns)."""
    rows = []
    for r in sorted(results, key=lambda x: x.score, reverse=True):
        row = {"Candidate": r.candidate.display_name, "Score": round(r.score)}
        for kw in jd.required:
            row[kw] = _keyword_cell(r, kw)
        row["GitHub"] = _github_cell(r)
        rows.append(row)
    return pd.DataFrame(rows)


def _render_candidate_detail(r: ScoreResult, jd: JDSpec) -> None:
    """Render the per-candidate visual breakdown inside an expander."""
    required_lower = {s.lower() for s in jd.required}

    # --- Required evidence, one strongest match per keyword ----------------
    st.markdown("**Required keyword evidence**")
    evidence = {e.skill: e for e in r.keyword_evidence}
    rows = []
    for kw in jd.required:
        e = evidence.get(kw)
        rows.append({"Keyword": kw, "Match": "❌ Missing" if not e else ("✅ Project" if e.match_type == "project" else "🟡 Skills"),
                     "Project": e.project_name if e else "", "Source": e.source if e else "",
                     "Project GitHub": e.project_github_url if e else "", "GitHub status": e.github_status.value if e else "",
                     "Verified": "Yes" if e and e.verified else "No"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("Score: " + " + ".join(f"{key} {value}" for key, value in r.score_breakdown.items()))

    if jd.preferred:
        st.markdown("**Preferred keywords**")
        pbadges = []
        for kw in jd.preferred:
            sections = r.matched_in.get(kw, [])
            if sections:
                where = "Projects" if "Projects" in sections else "Skills"
                pbadges.append(f":green-background[➕ {kw} · {where}]")
            else:
                pbadges.append(f":gray-background[{kw} · missing]")
        st.markdown("&nbsp;&nbsp;".join(pbadges))

    # --- GitHub breakdown --------------------------------------------------
    st.markdown("**GitHub**")
    if not r.github_found:
        st.markdown(":red[❌ No GitHub link found in the resume.]")
    elif not r.github_checked:
        for url in r.github_urls:
            st.markdown(f"- ⏸️ [{url}]({url}) — _not validated (GitHub check skipped)_")
    else:
        for link in r.github_links:
            emoji = _GH_EMOJI.get(link.status, "❓")
            st.markdown(
                f"- {emoji} [{link.url}]({link.url}) — "
                f"**{link.status.value}** · {link.kind.value}"
            )

    # --- Projects with detected technologies -------------------------------
    st.markdown("**Projects & detected technologies**")
    if not r.projects:
        st.caption("No projects section detected in this resume.")
        return
    for p in r.projects:
        title = p.name or "(unnamed project)"
        hits = [t for t in p.technologies if t.lower() in required_lower]
        marker = "✅" if hits else "▫️"
        st.markdown(f"{marker} **{title}**")
        if p.technologies:
            chips = "&nbsp;".join(
                f":green-background[{t}]"
                if t.lower() in required_lower
                else f":blue-background[{t}]"
                for t in p.technologies
            )
            st.markdown(chips)
        else:
            st.caption("No technologies detected.")
        links = []
        if p.github_url:
            links.append(f"[🔗 GitHub]({p.github_url})")
        if p.live_url:
            links.append(f"[🌐 Live]({p.live_url})")
        if links:
            st.markdown(" · ".join(links))


# --------------------------------------------------------------------------- #
# Sidebar — inputs
# --------------------------------------------------------------------------- #
st.sidebar.title("📄 Resume Shortlister")
st.sidebar.caption("Keyword-based, no LLM. Searches Skills **and** Projects.")

source_kind = st.sidebar.radio(
    "Candidate source", ["Google Sheet URL", "Upload Excel / CSV"]
)

sheet_url = ""
uploaded = None
if source_kind == "Google Sheet URL":
    sheet_url = st.sidebar.text_input(
        "Google Sheet URL",
        placeholder="https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0",
        help="The sheet must be shared: 'Anyone with the link can view'.",
    )
else:
    uploaded = st.sidebar.file_uploader(
        "Excel / CSV of candidates", type=["xlsx", "xls", "csv"]
    )

check_github = not st.sidebar.checkbox(
    "Skip GitHub validation (faster)", value=True,
    help="GitHub is rate-limited to 60 req/hr without a token. Keep this on "
    "for large sheets.",
)
limit = st.sidebar.number_input(
    "Limit candidates (0 = all)", min_value=0, value=0, step=10,
    help="Process only the first N rows — handy for a quick dry run.",
)

st.sidebar.divider()
st.sidebar.subheader("Recommendation bands")
st.sidebar.caption(
    f"Strong ≥ {config.BANDS.strong_shortlist} · "
    f"Shortlist ≥ {config.BANDS.shortlist} · "
    f"Consider ≥ {config.BANDS.consider}"
)

# --------------------------------------------------------------------------- #
# Main — keywords
# --------------------------------------------------------------------------- #
st.title("Shortlist candidates by keywords")

st.markdown(
    "Enter the keywords to search for in each resume. A required skill found in "
    "a **Project** scores higher than one merely listed under Skills."
)

suggestions = _known_skill_names()
col1, col2 = st.columns(2)
with col1:
    required = st.multiselect(
        "✅ Required keywords",
        options=suggestions,
        default=[],
        help="Known skills auto-complete; you can also type custom ones below.",
    )
    required_extra = st.text_input(
        "Add custom required keywords (comma-separated)",
        placeholder="e.g. Snowflake, dbt",
    )
with col2:
    preferred = st.multiselect(
        "➕ Preferred keywords (nice to have)",
        options=suggestions,
        default=[],
    )
    preferred_extra = st.text_input(
        "Add custom preferred keywords (comma-separated)",
        placeholder="e.g. Airflow",
    )

# Merge picker selections with free-text additions.
required_all = required + parse_keyword_string(required_extra)
preferred_all = preferred + parse_keyword_string(preferred_extra)
search_modes: dict[str, str] = {}

# Show the normalised JD spec so the user sees exactly what will be searched.
if required_all or preferred_all:
    jd_preview = build_jd_from_keywords(required_all, preferred_all, _kb())
    st.info(
        f"**Required:** {', '.join(jd_preview.required) or '—'}  \n"
        f"**Preferred:** {', '.join(jd_preview.preferred) or '—'}"
    )
    with st.expander("Required keyword search rules", expanded=False):
        st.caption("Project requires project evidence; Skills only accepts the Skills section; Skills or Project prefers project evidence.")
        for keyword in jd_preview.required:
            search_modes[keyword] = st.selectbox(
                keyword, ["skills", "project", "skills_or_project"],
                index=["skills", "project", "skills_or_project"].index(jd_preview.search_mode_for(keyword)),
                key=f"search_mode_{keyword}",
            )

run_clicked = st.button("🚀 Run shortlisting", type="primary", use_container_width=True)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def _build_source():
    """Create a ResumeSource from the selected input, or raise ValueError."""
    if source_kind == "Google Sheet URL":
        if not sheet_url.strip():
            raise ValueError("Please paste a Google Sheet URL.")
        return GoogleSheetSource(sheet_url.strip())
    if uploaded is None:
        raise ValueError("Please upload an Excel/CSV file.")
    # Persist the upload to a temp file so ExcelSource can read it by path.
    suffix = Path(uploaded.name).suffix or ".xlsx"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded.getbuffer())
    tmp.close()
    return ExcelSource(tmp.name)


if run_clicked:
    if not required_all:
        st.error("Add at least one **required** keyword before running.")
        st.stop()

    try:
        source = _build_source()
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    jd = build_jd_from_keywords(required_all, preferred_all, _kb(), search_modes)

    dl_bar = st.progress(0.0, text="Downloading resumes…")
    proc_bar = st.progress(0.0, text="Processing resumes…")

    def on_download(done: int, total: int) -> None:
        dl_bar.progress(done / total, text=f"Downloading resumes… {done}/{total}")

    def on_process(done: int, total: int) -> None:
        proc_bar.progress(done / total, text=f"Processing resumes… {done}/{total}")

    try:
        with st.spinner("Reading sheet and running the pipeline…"):
            outcome = run_shortlisting(
                source=source,
                jd=jd,
                kb=_kb(),
                check_github=check_github,
                limit=int(limit),
                write_outputs=True,
                download_progress=on_download,
                process_progress=on_process,
            )
    except Exception as exc:  # noqa: BLE001 - surface any failure to the user
        st.error(f"Run failed: {exc}")
        st.stop()

    dl_bar.empty()
    proc_bar.empty()

    results = outcome.results
    if not results:
        st.warning("No candidates were processed. Check the sheet contents / sharing.")
        st.stop()

    # ---- Summary metrics --------------------------------------------------
    st.success(f"Processed {len(results)} candidate(s).")
    m = st.columns(4)
    for col, band in zip(
        m, ["Strong Shortlist", "Shortlist", "Consider", "Reject"]
    ):
        col.metric(band, outcome.stats.get(band, 0))

    # ---- Match matrix (candidate × keyword, at a glance) ------------------
    st.subheader("🔎 Match matrix")
    st.caption(
        "✅ Project = keyword found in a project  ·  🟡 Skills = only in the "
        "skills list  ·  — = not found.  GitHub: ✅ Working / ❌ Broken / "
        "⚠️ Not found / 🔒 Private / 🚫 None / ⏸️ Not checked."
    )
    matrix = _keyword_matrix(results, outcome.jd)
    st.dataframe(
        matrix,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d"
            ),
        },
    )

    # ---- Per-candidate visual detail -------------------------------------
    st.subheader("🧑‍💻 Candidate details")
    st.caption("Expand a candidate to see exactly where each keyword matched.")
    for occurrence, r in enumerate(sorted(results, key=lambda x: x.score, reverse=True)):
        gh = _github_cell(r)
        with st.expander(
            f"{r.candidate.display_name}  —  {round(r.score)}/100  ·  "
            f"{r.recommendation}  ·  GitHub {gh}"
        ):
            _render_candidate_detail(r, outcome.jd)
            path = profile_path(r)
            if path:
                st.download_button(
                    "⬇️ Download Profile",
                    path.read_bytes(),
                    profile_name(r),
                    key=f"download_profile_{_candidate_widget_id(r, occurrence)}",
                    use_container_width=True,
                )
            else:
                st.caption("Original cached resume is unavailable for download.")

    # ---- Full results table (all columns, sortable) ----------------------
    df = to_dataframe(results)
    st.subheader("📋 Full results table")
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Matching Score": st.column_config.ProgressColumn(
                "Matching Score", min_value=0, max_value=100, format="%d"
            ),
            "Resume URL": st.column_config.LinkColumn("Resume URL"),
        },
    )

    # ---- Downloads --------------------------------------------------------
    xlsx_buf = io.BytesIO()
    with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Shortlist")
    json_bytes = (
        outcome.json_path.read_bytes() if outcome.json_path else b"{}"
    )

    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇️ Download Excel",
        data=xlsx_buf.getvalue(),
        file_name="final_shortlisted.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    d2.download_button(
        "⬇️ Download JSON",
        data=json_bytes,
        file_name="report.json",
        mime="application/json",
        use_container_width=True,
    )
    st.subheader("Profile downloads")
    categories = st.multiselect("Recommendation categories", ["Strong Shortlist", "Shortlist", "Consider", "Reject"], default=["Strong Shortlist", "Shortlist", "Consider"])
    evidence_filters = st.multiselect("Evidence filters", ["Project Match", "Skills Match", "Verified Project", "Missing Required Keyword", "GitHub Working", "GitHub Missing", "GitHub Broken"])
    def matches_filters(r):
        evidence = r.keyword_evidence
        flags = {
            "Project Match": any(e.match_type == "project" for e in evidence),
            "Skills Match": any(e.match_type == "skills" for e in evidence),
            "Verified Project": any(e.verified for e in evidence),
            "Missing Required Keyword": bool(r.missing_skills),
            "GitHub Working": any(e.github_status is GithubStatus.WORKING for e in evidence),
            "GitHub Missing": any(e.match_type == "project" and not e.project_github_url for e in evidence),
            "GitHub Broken": any(e.github_status in (GithubStatus.BROKEN, GithubStatus.NOT_FOUND) for e in evidence),
        }
        return not evidence_filters or all(flags[name] for name in evidence_filters)
    shown = [r for r in results if r.recommendation in categories and matches_filters(r)]
    selection_keys = [f"profile_{_candidate_widget_id(r, i)}" for i, r in enumerate(shown)]
    controls = st.columns(5)
    if controls[0].button("Select All"):
        for key in selection_keys: st.session_state[key] = True
    if controls[1].button("Clear Selection"):
        for key in selection_keys: st.session_state[key] = False
    for col, label in zip(controls[2:], ["Strong Shortlist", "Shortlist", "Consider"]):
        if col.button(f"Select {label}"):
            for key, result in zip(selection_keys, shown): st.session_state[key] = result.recommendation == label
    selected = []
    for i, r in enumerate(shown):
        if st.checkbox(f"{r.candidate.display_name} — {r.recommendation}", key=selection_keys[i]):
            selected.append(r)
    st.caption(f"{len(selected)} profiles selected")
    st.download_button("⬇️ Download Selected Profiles", profiles_zip(selected) if selected else b"", "shortlisted_profiles.zip", "application/zip", disabled=not selected, use_container_width=True)
    st.download_button("⬇️ Download Shortlist CSV", df.to_csv(index=False).encode("utf-8-sig"), "shortlist.csv", "text/csv", use_container_width=True)
else:
    st.caption(
        "Configure the source and keywords in the sidebar, then click "
        "**Run shortlisting**."
    )
