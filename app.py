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
from resume_shortlisting.coding_profiles import (
    TABLE_COLUMNS as CODING_COLUMNS,
    PLATFORM_LABELS,
    available_profiles,
    stats_summary,
)
from resume_shortlisting.excel_writer import to_dataframe
from resume_shortlisting.models import GithubStatus, JDSpec, ScoreResult
from resume_shortlisting.skills_kb import load_kb
from resume_shortlisting.sources import ExcelSource, GoogleSheetSource, TalentPoolSource
from resume_shortlisting.profile_exports import profile_name, profile_path, profiles_zip
from resume_shortlisting.talent_pool import run_talent_pool, talent_pool_xlsx_bytes, to_dataframe as talent_pool_dataframe
from resume_shortlisting.utils import setup_logging, url_hash

st.set_page_config(page_title="Resume Shortlisting", page_icon="📄", layout="wide")

setup_logging(to_console=False)
config.ensure_dirs()


def _render_talent_pool() -> None:
    """Separate Student Talent Pool UI; it never invokes JD shortlisting."""
    st.title("Student Talent Pool")
    st.caption("Evidence-based, role-specific capability profiling. Interest fields are preserved but do not affect scores.")
    source_kind = st.sidebar.radio("Talent-pool source", ["Google Sheet URL", "Upload Excel / CSV"], key="talent_source")
    sheet_url = st.sidebar.text_input("Google Sheet URL", key="talent_sheet") if source_kind == "Google Sheet URL" else ""
    upload = st.sidebar.file_uploader("Excel / CSV of students", type=["xlsx", "xls", "csv"], key="talent_upload") if source_kind != "Google Sheet URL" else None
    check_github = not st.sidebar.checkbox("Skip GitHub verification (faster)", value=True, key="talent_github")
    if st.button("Process / Analyze Students", type="primary"):
        try:
            if source_kind == "Google Sheet URL":
                if not sheet_url.strip(): raise ValueError("Please paste a Google Sheet URL.")
                source = GoogleSheetSource(sheet_url.strip())
            else:
                if upload is None: raise ValueError("Please upload an Excel or CSV file.")
                temp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(upload.name).suffix or ".xlsx")
                temp.write(upload.getbuffer()); temp.close(); source = ExcelSource(temp.name)
            students = TalentPoolSource(source).read()
            progress = st.progress(0.0, text="Processing student profiles…")
            profiles = run_talent_pool(students, check_github=check_github, on_progress=lambda done, total: progress.progress(done / total, text=f"Processing student profiles… {done}/{total}"))
            progress.empty(); st.session_state["talent_pool_profiles"] = profiles
            # Streamlit receives the same in-memory bytes that were ZIP and
            # openpyxl validated; no CSV/text or partially-written file path.
            export_bytes = talent_pool_xlsx_bytes(profiles)
            output = config.OUTPUTS_DIR / "student_talent_pool.xlsx"
            output.write_bytes(export_bytes)
            st.session_state["talent_pool_export"] = export_bytes
        except Exception as exc:
            st.error(f"Talent-pool run failed: {exc}")
    profiles = st.session_state.get("talent_pool_profiles")
    if not profiles:
        st.info("Choose a student source and explicitly start processing.")
        return
    frame = talent_pool_dataframe(profiles)
    roles = [column[:-6] for column in frame.columns if column.endswith(" Score")]
    role = st.selectbox("Filter role", ["All"] + roles)
    tiers = st.multiselect("Tier", ["A", "B", "C", "D"], default=["A", "B", "C", "D"])
    ready = st.multiselect("Target Ready", ["YES", "NO", "NEEDS REVIEW"], default=["YES", "NO", "NEEDS REVIEW"])
    priorities = st.multiselect("Target Priority", ["A — Target First", "B — Strong Candidate", "C — Developing", "D — Low Evidence"], default=["A — Target First", "B — Strong Candidate", "C — Developing", "D — Low Evidence"])
    github_verified = st.checkbox("GitHub verified only", key="talent_verified")
    minimum_projects = st.number_input("Minimum relevant projects", min_value=0, value=0, step=1, key="talent_projects")
    view = frame if role == "All" else frame[frame[f"{role} Tier"].isin(tiers)]
    view = view[view["Target Ready"].isin(ready) & view["Target Priority"].isin(priorities) & (view["Relevant Projects"] >= minimum_projects)]
    if github_verified: view = view[view["GitHub Verification Status"] == "Verified"]
    st.success(f"Processed {len(profiles)} student(s). Showing {len(view)}.")
    st.dataframe(view, hide_index=True, width="stretch")
    for profile in profiles:
        with st.expander(f"{profile.candidate.display_name} — {profile.target_priority}"):
            st.write({name: {"score": score.score, "tier": score.tier, "Skill Match": score.skill_score, "Relevant Projects": score.project_score, "GitHub Evidence": score.github_score, "Resume/Project Evidence": score.evidence_score, "Matched skills": score.matched_required_skills + score.matched_preferred_skills, "Relevant projects": score.relevant_projects} for name, score in profile.role_scores.items()})
    st.download_button("Download Talent Pool XLSX", st.session_state.get("talent_pool_export", b""), "student_talent_pool.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.caption("The XLSX is Google Sheets-compatible; import it into Google Sheets. Direct Sheets writing is not configured.")


app_mode = st.sidebar.radio("Module", ["Resume Analyzer", "Student Talent Pool"], key="app_module")
if app_mode == "Student Talent Pool":
    _render_talent_pool()
    st.stop()


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


def _stored_candidate_id(result: ScoreResult) -> str:
    """Return the per-run stable ID assigned when the pipeline completed."""
    return st.session_state.get("shortlisting_result_ids", {}).get(
        id(result), _candidate_widget_id(result)
    )


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
    if "Experience" in sections:
        return "🔎 Experience"
    if "Whole Resume" in sections:
        return "🔎 Whole Resume"
    return "—"


def _has_discovery_match(result: ScoreResult) -> bool:
    """Return whether a result contains non-scoring discovery evidence."""
    return any(
        evidence.match_type in ("experience", "whole_resume")
        for evidence in result.keyword_evidence
    )


def _result_sort_key(result: ScoreResult) -> tuple[bool, float]:
    """Place discovery matches first, then preserve score ordering."""
    return (_has_discovery_match(result), result.score if result.score is not None else -1)


def _github_cell(result: ScoreResult) -> str:
    """Matrix cell: the candidate's GitHub headline status."""
    if not result.github_found:
        return "🚫 None"
    if not result.github_checked:
        return "⏸️ Not checked"
    return _GH_MATRIX_CELL.get(result.github_status, "❓")


def _keyword_matrix(results: list[ScoreResult], jd: JDSpec) -> pd.DataFrame:
    """Build the candidate × keyword grid (+ Score and GitHub columns).

    Coding-profile evidence deliberately lives in the full results table and
    the candidate detail view, not here — the matrix stays a keyword grid.
    """
    rows = []
    for r in sorted(results, key=_result_sort_key, reverse=True):
        row = {"Candidate": r.candidate.display_name, "Score": round(r.score) if r.score is not None else "N/A"}
        for kw in jd.required:
            row[kw] = _keyword_cell(r, kw)
        row["GitHub"] = _github_cell(r)
        rows.append(row)
    return pd.DataFrame(rows)


def _render_coding_profiles(r: ScoreResult) -> None:
    """Public coding-platform evidence. Informational only — never scored."""
    st.markdown("**👨‍💻 Coding Profiles**")
    found = available_profiles(r.coding_profiles)
    if not found:
        st.caption("No public coding profile was found for this candidate.")
        return
    for profile in found:
        line = (
            f"✅ **{PLATFORM_LABELS[profile.platform]}** — "
            f"[🔗 Profile]({profile.profile_url})"
        )
        if profile.problems_solved is not None:
            line += f" · Solved: **{profile.problems_solved}**"
        if profile.rating is not None:
            line += f" · Rating: **{profile.rating}**"
        if profile.problems_solved is None and profile.rating is None:
            line += f" · :gray[{stats_summary(profile)}]"
        st.markdown(line)
    st.caption("Coding-profile evidence is informational and does not affect the score or recommendation.")


def _render_candidate_detail(r: ScoreResult, jd: JDSpec) -> None:
    """Render the per-candidate visual breakdown inside an expander."""
    required_lower = {s.lower() for s in jd.required}

    # --- Required evidence, one strongest match per keyword ----------------
    st.markdown("**Required keyword evidence**")
    evidence = {e.skill: e for e in r.keyword_evidence}
    rows = []
    for kw in jd.required:
        e = evidence.get(kw)
        match_labels = {
            "project": "✅ Project",
            "skills": "🟡 Skills",
            "experience": "🔎 Experience",
            "whole_resume": "🔎 Whole Resume",
        }
        rows.append({"Keyword": kw, "Match": "❌ Missing" if not e else match_labels[e.match_type],
                     "Project": e.project_name if e else "", "Source": e.source if e else "",
                     "Project GitHub": e.project_github_url if e else "", "GitHub status": e.github_status.value if e else "",
                     "Verified": "Yes" if e and e.verified else "No"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Score: " + " + ".join(f"{key} {value}" for key, value in r.score_breakdown.items()))

    if jd.preferred:
        st.markdown("**Preferred keywords**")
        pbadges = []
        for kw in jd.preferred:
            sections = r.matched_in.get(kw, [])
            if sections:
                where = next((section for section in ("Projects", "Skills", "Experience", "Whole Resume") if section in sections), "")
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

    # --- Coding profiles (information only) --------------------------------
    _render_coding_profiles(r)

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
fetch_coding_stats = not st.sidebar.checkbox(
    "Skip coding-profile stats (faster)", value=False,
    help="Solved counts are fetched from the public LeetCode/Codeforces/CodeChef "
    "profiles by default. Tick this to skip those requests on a large sheet — "
    "profile links are still detected either way.",
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
                keyword, ["skills", "project", "skills_or_project", "experience", "whole_resume"],
                format_func=lambda mode: {
                    "skills": "Skills",
                    "project": "Projects",
                    "skills_or_project": "Skills + Projects",
                    "experience": "Experience",
                    "whole_resume": "Whole Resume",
                }[mode],
                index=["skills", "project", "skills_or_project", "experience", "whole_resume"].index(jd_preview.search_mode_for(keyword)),
                key=f"search_mode_{keyword}",
            )

run_clicked = st.button("🚀 Run shortlisting", type="primary", width="stretch")


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
                fetch_coding_stats=fetch_coding_stats,
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
    if not outcome.results:
        st.warning("No candidates were processed. Check the sheet contents / sharing.")
        st.stop()
    # The run result (including parsed evidence and generated files) is the
    # single source of truth for every later UI rerun.
    st.session_state["shortlisting_outcome"] = outcome
    st.session_state["shortlisting_config"] = {
        "required": required_all, "preferred": preferred_all, "search_modes": search_modes,
    }
    st.session_state["shortlisting_result_ids"] = {
        id(result): f"{_candidate_widget_id(result)}_{index}"
        for index, result in enumerate(outcome.results)
    }
    st.session_state["selected_candidate_ids"] = set()
    st.session_state["final_candidate_sheet_bytes"] = (
        outcome.excel_path.read_bytes() if outcome.excel_path else b""
    )
    st.session_state["shortlisting_json_bytes"] = (
        outcome.json_path.read_bytes() if outcome.json_path else b"{}"
    )

outcome = st.session_state.get("shortlisting_outcome")
if outcome is not None:
    results = outcome.results
    current_config = {
        "required": required_all, "preferred": preferred_all, "search_modes": search_modes,
    }
    if current_config != st.session_state.get("shortlisting_config"):
        st.info("Configuration changed. Click Run shortlisting to process with the new settings; displayed results are from the previous run.")

    # ---- Summary metrics --------------------------------------------------
    # Processing status counts
    # Keep this compatible even if RunResult does not expose status_counts.
    status_counts = getattr(outcome, "status_counts", None)

    if status_counts is None:
        status_counts = {}

        for result in outcome.results:
            status = getattr(result, "processing_status", "Analyzed")
            status_counts[status] = status_counts.get(status, 0) + 1

    analyzed_count = status_counts.get("Analyzed", 0)

    download_access_failed = (
        status_counts.get("Download Failed", 0)
        + status_counts.get("Access Denied", 0)
    )

    extraction_analysis_failed = (
        status_counts.get("Extraction Failed", 0)
        + status_counts.get("Analysis Failed", 0)
    )

    whole_resume_matches = sum(
        any(evidence.match_type == "whole_resume" for evidence in result.keyword_evidence)
        for result in results
    )
    experience_matches = sum(
        any(evidence.match_type == "experience" for evidence in result.keyword_evidence)
        for result in results
    )

    st.success(
        f"Total candidates: {len(outcome.results)} · "
        f"Analyzed: {analyzed_count} · "
        f"Download/access failed: {download_access_failed} · "
        f"Extraction/analysis failed: {extraction_analysis_failed}"
    )
    st.caption(
        f"Whole Resume matches: {whole_resume_matches} · "
        f"Experience matches: {experience_matches}"
    )
    m = st.columns(4)
    for col, band in zip(
        m, ["Strong Shortlist", "Shortlist", "Consider", "Reject"]
    ):
        col.metric(band, outcome.stats.get(band, 0))

    st.subheader("Result filters")
    categories = st.multiselect(
        "Recommendation categories",
        ["Strong Shortlist", "Shortlist", "Consider", "Reject", "Not Evaluated"],
        default=["Strong Shortlist", "Shortlist", "Consider", "Reject", "Not Evaluated"],
        key="result_status_filter",
    )
    evidence_filters = st.multiselect(
        "Evidence filters",
        ["Project Match", "Skills Match", "Verified Project", "Missing Required Keyword", "GitHub Working", "GitHub Missing", "GitHub Broken"],
        key="result_evidence_filter",
    )

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

    displayed_results = [
        r for r in results
        if (_has_discovery_match(r) or r.recommendation in categories or (r.recommendation == "N/A" and "Not Evaluated" in categories))
        and matches_filters(r)
    ]
    st.caption(f"Showing {len(displayed_results)} of {len(results)} processed candidates.")

    # ---- Match matrix (candidate × keyword, at a glance) ------------------
    st.subheader("🔎 Match matrix")
    st.caption(
        "✅ Project = keyword found in a project  ·  🟡 Skills = only in the "
        "skills list  ·  — = not found.  GitHub: ✅ Working / ❌ Broken / "
        "⚠️ Not found / 🔒 Private / 🚫 None / ⏸️ Not checked."
    )
    matrix = _keyword_matrix(displayed_results, outcome.jd)
    st.dataframe(
        matrix,
        width="stretch",
        hide_index=True,
        column_config={
            "Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d"
            ),
        },
    )

    # ---- Per-candidate visual detail -------------------------------------
    with st.expander("🧑‍💻 Candidate details", expanded=False):
        st.caption("Expand a candidate to see exactly where each keyword matched.")
        with st.container(height=620, border=False):
            for result in sorted(displayed_results, key=_result_sort_key, reverse=True):
                gh = _github_cell(result)
                with st.expander(
                    f"{result.candidate.display_name}  —  "
                    f"{round(result.score) if result.score is not None else 'N/A'}/100  ·  "
                    f"{result.processing_status}  ·  {result.recommendation}  ·  GitHub {gh}"
                ):
                    _render_candidate_detail(result, outcome.jd)
                    path = profile_path(result)
                    if path:
                        st.download_button(
                            "⬇️ Download Profile",
                            path.read_bytes(),
                            profile_name(result),
                            key=f"download_profile_{_stored_candidate_id(result)}",
                            width="stretch",
                        )
                    else:
                        st.caption("Original cached resume is unavailable for download.")

    # ---- Full results table (all columns, sortable) ----------------------
    df = to_dataframe(results)
    view_df = to_dataframe(displayed_results)
    st.subheader("📋 Full results table")
    st.dataframe(
        view_df,
        width="stretch",
        hide_index=True,
        column_config={
            "Matching Score": st.column_config.ProgressColumn(
                "Matching Score", min_value=0, max_value=100, format="%d"
            ),
            "Resume URL": st.column_config.LinkColumn("Resume URL"),
            # Coding-profile links stay clickable without showing a long URL.
            **{
                column: st.column_config.LinkColumn(column, display_text="🔗 Profile")
                for column in CODING_COLUMNS if column.endswith(" Profile")
            },
        },
    )

    # ---- Downloads --------------------------------------------------------
    final_sheet_bytes = st.session_state["final_candidate_sheet_bytes"]
    json_bytes = st.session_state["shortlisting_json_bytes"]

    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇️ Download Final Candidate Sheet",
        data=final_sheet_bytes,
        file_name="final_candidate_sheet.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )
    d2.download_button(
        "⬇️ Download JSON",
        data=json_bytes,
        file_name="report.json",
        mime="application/json",
        width="stretch",
    )
    st.caption("Direct Google Sheets writing is not configured; download the final XLSX or CSV and import it into Google Sheets.")
    with st.expander("Profile downloads", expanded=False):
        selected_ids = st.session_state.setdefault("selected_candidate_ids", set())
        shown = displayed_results
        selection_keys = [f"profile_{_stored_candidate_id(r)}" for r in shown]
        all_selection_keys = [f"profile_{_stored_candidate_id(r)}" for r in results]
        controls = st.columns(5)
        if controls[0].button("Select All"):
            for key, result in zip(selection_keys, shown):
                st.session_state[key] = True
                selected_ids.add(_stored_candidate_id(result))
        if controls[1].button("Clear Selection"):
            for key in all_selection_keys: st.session_state[key] = False
            selected_ids.clear()
        for col, label in zip(controls[2:], ["Strong Shortlist", "Shortlist", "Consider"]):
            if col.button(f"Select {label}"):
                for result in results:
                    key = f"profile_{_stored_candidate_id(result)}"
                    chosen = result.recommendation == label
                    st.session_state[key] = chosen
                    candidate_id = _stored_candidate_id(result)
                    if chosen:
                        selected_ids.add(candidate_id)
                    else:
                        selected_ids.discard(candidate_id)
        with st.container(height=420, border=False):
            for i, r in enumerate(shown):
                candidate_id = _stored_candidate_id(r)
                st.session_state.setdefault(selection_keys[i], candidate_id in selected_ids)
                if st.checkbox(f"{r.candidate.display_name} — {r.recommendation}", key=selection_keys[i]):
                    selected_ids.add(candidate_id)
                else:
                    selected_ids.discard(candidate_id)
        selected = [r for r in results if _stored_candidate_id(r) in selected_ids]
        st.caption(f"{len(selected)} profiles selected")
        st.download_button("⬇️ Download Selected Profiles", profiles_zip(selected) if selected else b"", "shortlisted_profiles.zip", "application/zip", disabled=not selected, width="stretch")
        st.download_button("⬇️ Download Shortlist CSV", df.to_csv(index=False).encode("utf-8-sig"), "shortlist.csv", "text/csv", width="stretch")
else:
    st.caption(
        "Configure the source and keywords in the sidebar, then click "
        "**Run shortlisting**."
    )
