"""
app.py — Streamlit frontend for the PPP Candidate Research Agent.

Run with: streamlit run app.py
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PPP Candidate Research Agent",
    page_icon="🔍",
    layout="centered",
)

REQUIRED_COLUMNS = {"full_name", "current_employer", "current_title", "linkedin_url"}

ORIGINAL_CANDIDATES = {
    "Andrew Swan", "Cathy Hales", "Jason Ennis", "Deborah Southon", "Nikki Thomas"
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_csv(file_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Parse uploaded CSV bytes. Returns (rows, errors)."""
    try:
        text = file_bytes.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            return [], ["CSV file is empty or has no header row."]

        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            return [], [f"Missing required columns: {', '.join(sorted(missing))}"]

        rows = [dict(row) for row in reader]
        if not rows:
            return [], ["CSV has no data rows."]

        return rows, []
    except Exception as e:
        return [], [f"Could not parse CSV: {e}"]


def score_color(score: int, max_score: int) -> str:
    if max_score == 5:
        if score <= 2:
            return "green"
        elif score == 3:
            return "orange"
        else:
            return "red"
    else:  # max_score == 10
        if score >= 8:
            return "green"
        elif score >= 5:
            return "orange"
        else:
            return "red"


def render_score_badge(label: str, score: int, max_score: int) -> None:
    color = score_color(score, max_score)
    st.markdown(
        f"<span style='color:{color};font-size:1.3em;font-weight:bold'>"
        f"{score}/{max_score}</span> &nbsp; <b>{label}</b>",
        unsafe_allow_html=True,
    )


def write_wrapped_text(text: str) -> None:
    """Render long strings without horizontal overflow (URLs, unbroken clauses)."""
    safe = html.escape(text or "")
    st.markdown(
        f'<div style="max-width:100%;overflow-wrap:anywhere;word-break:break-word;">{safe}</div>',
        unsafe_allow_html=True,
    )


def _parallel_run_settings(n: int) -> tuple[int, float]:
    """
    How many candidates to process at once and how far apart to start them.

    Default max_workers=1 avoids stacking Anthropic *input* tokens per minute (org TPM),
    which is the usual failure mode when moving from sequential to parallel.
    Override with PPP_MAX_PARALLEL and optional PPP_STAGGER_SEC in .env.
    """
    raw = os.getenv("PPP_MAX_PARALLEL", "1")
    try:
        max_p = max(1, int(raw))
    except ValueError:
        max_p = 1
    max_workers = min(n, max_p)

    stagger_raw = os.getenv("PPP_STAGGER_SEC")
    if stagger_raw is None:
        # Spread starts when running >1 in parallel to reduce TPM bursts.
        stagger = 5.0 if max_workers > 1 else 0.0
    else:
        try:
            stagger = max(0.0, float(stagger_raw))
        except ValueError:
            stagger = 0.0

    return max_workers, stagger


def run_pipeline(candidates: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """
    Run the agent pipeline for all candidates.
    Updates st.session_state.statuses in real time.
    Returns (results, failures).
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    from agent import run_candidate_agent
    from tools.write_output import write_output

    results = []
    failures = []

    max_workers, stagger_sec = _parallel_run_settings(len(candidates))

    # Reset output.json
    output_path = Path(__file__).parent / "output" / "output.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"candidates": []}, f)

    # Clear .tmp/
    tmp_dir = Path(__file__).parent / ".tmp"
    if tmp_dir.exists():
        import shutil
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Mark all as researching upfront
    for i in range(len(candidates)):
        st.session_state.statuses[i] = "Researching..."
    _refresh_status(candidates)

    def _process(i: int, candidate: dict) -> tuple[int, dict | None, tuple | None]:
        """Run one candidate; returns (index, result, failure). Runs in a thread."""
        try:
            result = run_candidate_agent(candidate, i)
            return i, result, None
        except Exception as e:
            return i, None, (candidate.get("full_name", f"Candidate {i}"), str(e))

    # Cap workers at 3 to reduce search/API pressure; honour PPP_MAX_PARALLEL below that cap
    max_workers = min(len(candidates), max_workers, 3)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_process, i, candidate): i
            for i, candidate in enumerate(candidates, 1)
        }

        for future in as_completed(future_map):
            i, result, failure = future.result()
            candidate = candidates[i - 1]
            name = candidate.get("full_name", f"Candidate {i}")

            if failure:
                failures.append(failure)
                st.session_state.statuses[i - 1] = "Error"
                st.warning(f"**{name}** — `{failure[1]}`")
                try:
                    from agent import _minimal_error_object
                    err_obj = _minimal_error_object(i, name, failure[1])
                    write_output(err_obj)
                except Exception:
                    pass

            elif result is not None:
                if result.get("career_narrative", "").startswith("[ERROR]"):
                    reason = result["career_narrative"]
                    failures.append((name, reason))
                    st.session_state.statuses[i - 1] = "Error"
                    st.warning(f"**{name}** — {reason[:900]}")
                    # Agent did not write this error object — write it now
                    write_output(result)
                else:
                    # Agent already wrote via write_output tool — don't write again
                    results.append(result)
                    st.session_state.statuses[i - 1] = "Done ✓"

            _refresh_status(candidates)

    return results, failures


def _render_status_table(candidates: list[dict], statuses: list[str]) -> None:
    rows = []
    for i, (c, s) in enumerate(zip(candidates, statuses), 1):
        rows.append(f"| {i} | {c.get('full_name','')} | {s} |")
    table = "| # | Name | Status |\n|---|------|--------|\n" + "\n".join(rows)
    st.markdown(table)


def _refresh_status(candidates: list[dict]) -> None:
    if "status_placeholder" in st.session_state:
        st.session_state.status_placeholder.empty()
        with st.session_state.status_placeholder.container():
            _render_status_table(candidates, st.session_state.statuses)


# ── Main UI ────────────────────────────────────────────────────────────────────

st.title("PPP Candidate Research Agent")
st.caption(
    "Upload a CSV of candidates to generate structured intelligence briefings "
    "for use in executive search. "
    "Parallel runs use PPP_MAX_PARALLEL in .env (default 1) to stay under Anthropic token-per-minute limits."
)

# ── Upload section ─────────────────────────────────────────────────────────────
uploaded_file = st.file_uploader("Upload candidates CSV", type=["csv"])

candidates: list[dict] = []
csv_valid = False

if uploaded_file is not None:
    raw_bytes = uploaded_file.read()
    candidates, parse_errors = parse_csv(raw_bytes)

    if parse_errors:
        for err in parse_errors:
            st.error(err)
    else:
        csv_valid = True
        st.success(f"Loaded {len(candidates)} candidate(s).")

        # Preview table
        preview_data = [
            {
                "#": i,
                "Name": c.get("full_name", ""),
                "Employer": c.get("current_employer", ""),
                "Title": c.get("current_title", ""),
            }
            for i, c in enumerate(candidates, 1)
        ]
        st.table(preview_data)

# ── Run button ─────────────────────────────────────────────────────────────────
if csv_valid and candidates:
    if st.button("Generate briefings", type="primary"):

        # Initialise session state for status tracking
        st.session_state.statuses = ["Waiting"] * len(candidates)

        st.subheader("Progress")
        
        st.session_state.status_placeholder = st.empty()

        with st.session_state.status_placeholder.container():
            _render_status_table(candidates, st.session_state.statuses)

        # Check API key
        if not os.getenv("ANTHROPIC_API_KEY"):
            st.error(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to a .env file in the project root."
            )
            st.stop()

        _mw, _st = _parallel_run_settings(len(candidates))
        if _mw > 1:
            st.info(
                f"Running up to **{_mw}** candidates in parallel "
                f"(stagger {_st:.0f}s between starts). "
                f"If you see 429 rate-limit errors, set `PPP_MAX_PARALLEL=1` in `.env`."
            )

        with st.spinner("Running research agent..."):
            results, failures = run_pipeline(candidates)

        st.success(f"Complete: {len(results)} succeeded, {len(failures)} failed.")

        # ── Failures ──────────────────────────────────────────────────────────
        if failures:
            st.subheader("Failures")
            for name, reason in failures:
                st.warning(f"**{name}**: {reason[:800]}")

        # ── Results cards ──────────────────────────────────────────────────────
        if results:
            st.subheader("Candidate Briefings")

            for result in results:
                name = result.get("full_name", "Unknown")
                is_supplementary = name not in ORIGINAL_CANDIDATES

                with st.expander(
                    f"{'⭐ ' if is_supplementary else ''}{name}",
                    expanded=False,
                ):
                    if is_supplementary:
                        st.caption("*Supplementary candidate added by submitter*")

                    # Career narrative
                    st.markdown("**Career Narrative**")
                    st.write(result.get("career_narrative", ""))

                    # Experience tags
                    tags = result.get("experience_tags", [])
                    st.markdown("**Experience Tags**")
                    st.write(", ".join(tags))


                    # AUM context (often long URLs / figures — wrap to avoid layout overflow)
                    st.markdown("**Firm AUM Context**")
                    write_wrapped_text(result.get("firm_aum_context", ""))

                    st.divider()

                    col1, col2 = st.columns(2)

                    with col1:
                        mobility = result.get("mobility_signal", {})
                        m_score = mobility.get("score", 0)
                        render_score_badge("Mobility", m_score, 5)
                        st.caption(mobility.get("rationale", ""))

                    with col2:
                        role_fit = result.get("role_fit", {})
                        r_score = role_fit.get("score", 0)
                        render_score_badge("Role Fit", r_score, 10)
                        st.caption(role_fit.get("justification", ""))

                    st.divider()

                    # Outreach hook
                    st.markdown("**Outreach Hook**")
                    st.info(result.get("outreach_hook", ""))

        # ── Download button ────────────────────────────────────────────────────
        output_path = Path(__file__).parent / "output" / "output.json"
        if output_path.exists():
            st.divider()
            st.subheader("Download")
            with open(output_path, "r", encoding="utf-8") as f:
                output_json = f.read()

            st.download_button(
                label="Download output.json",
                data=output_json,
                file_name="output.json",
                mime="application/json",
                type="primary",
            )
