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

# ── Global CSS ─────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    html, body, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                     "Helvetica Neue", Arial, sans-serif;
        font-size: 15px;
        color: #1a1a2e;
    }

    h1 { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.3px; }

    .section-label {
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: #6b7280;
        margin-bottom: 4px;
    }

    .score-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 12px 4px 8px;
        border-radius: 20px;
        font-size: 0.95rem;
        font-weight: 700;
        margin-bottom: 8px;
    }
    .score-chip .num { font-size: 1.3rem; line-height: 1; }
    .score-chip.green  { background:#dcfce7; color:#15803d; }
    .score-chip.orange { background:#fff7ed; color:#c2410c; }
    .score-chip.red    { background:#fee2e2; color:#b91c1c; }

    .col-title {
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        color: #374151;
        margin-bottom: 6px;
        padding-bottom: 4px;
        border-bottom: 2px solid #e5e7eb;
    }

    .col-body {
        font-size: 0.88rem;
        line-height: 1.55;
        color: #ffffff;
    }

    .tag {
        display: inline-block;
        background: #f3f4f6;
        border: 1px solid #e5e7eb;
        border-radius: 4px;
        padding: 2px 9px;
        font-size: 0.78rem;
        font-weight: 500;
        color: #374151;
        margin: 2px 3px 2px 0;
    }

    .candidate-meta {
        font-size: 0.82rem;
        color: #6b7280;
        margin-top: -4px;
        margin-bottom: 12px;
    }

    .streamlit-expanderContent { padding-top: 0.5rem !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# linkedin_url is optional — rows without it are still processed.
REQUIRED_COLUMNS = {"full_name", "current_employer", "current_title"}

# Pause between candidates to let the Anthropic TPM window recover.
BETWEEN_CANDIDATES_DELAY = 10  # seconds

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


def _chip_class(score: int, max_score: int) -> str:
    if max_score == 5:
        return "green" if score <= 2 else ("orange" if score == 3 else "red")
    else:
        return "green" if score >= 8 else ("orange" if score >= 5 else "red")


def render_score_chip(score: int, max_score: int) -> None:
    cls = _chip_class(score, max_score)
    st.markdown(
        f"<div class='score-chip {cls}'>"
        f"<span class='num'>{score}</span>"
        f"<span>/ {max_score}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_tags(tags: list[str]) -> None:
    chips = "".join(f"<span class='tag'>{html.escape(t)}</span>" for t in tags)
    st.markdown(f"<div>{chips}</div>", unsafe_allow_html=True)


def section_label(text: str) -> None:
    st.markdown(f"<div class='section-label'>{text}</div>", unsafe_allow_html=True)


def write_wrapped_text(text: str) -> None:
    """Render long strings without horizontal overflow (URLs, unbroken clauses)."""
    safe = html.escape(text or "")
    st.markdown(
        f'<div style="font-size:0.88rem;line-height:1.55;'
        f'overflow-wrap:anywhere;word-break:break-word;">{safe}</div>',
        unsafe_allow_html=True,
    )


# ── Background pipeline worker ─────────────────────────────────────────────────

def _pipeline_worker(
    candidates: list[dict],
    shared: dict,
    stop_event: Any,
) -> None:
    """
    Background thread: runs the agent pipeline sequentially and writes results
    into *shared*. Checks stop_event between candidates and during delays.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    from agent import _minimal_error_object, run_candidate_agent
    from tools.write_output import write_output

    results: list[dict] = []
    failures: list[tuple[str, str]] = []
    total = len(candidates)

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

    for i, candidate in enumerate(candidates, 1):
        if stop_event.is_set():
            for idx in range(i - 1, total):
                shared["statuses"][idx] = "Stopped"
            break

        name = candidate.get("full_name", f"Candidate {i}")
        shared["statuses"][i - 1] = "Researching..."

        try:
            result = run_candidate_agent(candidate, i)

            if result.get("career_narrative", "").startswith("[ERROR]"):
                reason = result["career_narrative"]
                failures.append((name, reason))
                shared["statuses"][i - 1] = "Error"
                write_output(result)
            else:
                results.append(result)
                shared["statuses"][i - 1] = "Done ✓"

        except Exception as e:
            failures.append((name, str(e)))
            shared["statuses"][i - 1] = "Error"
            try:
                err_obj = _minimal_error_object(i, name, str(e))
                write_output(err_obj)
            except Exception:
                pass

        if i < total and not stop_event.is_set():
            deadline = time.time() + BETWEEN_CANDIDATES_DELAY
            while time.time() < deadline and not stop_event.is_set():
                time.sleep(min(0.5, deadline - time.time()))

    shared["results"] = results
    shared["failures"] = failures
    shared["done"] = True


# ── Status table ───────────────────────────────────────────────────────────────

def _render_status_table(candidates: list[dict], statuses: list[str]) -> None:
    rows = []
    for i, (c, s) in enumerate(zip(candidates, statuses), 1):
        rows.append(f"| {i} | {c.get('full_name','')} | {s} |")
    table = "| # | Name | Status |\n|---|------|--------|\n" + "\n".join(rows)
    st.markdown(table)



# ── Result card ────────────────────────────────────────────────────────────────

def _render_result_card(result: dict) -> None:
    """Render a single candidate briefing card inside an expander."""
    name = result.get("full_name", "Unknown")
    role = result.get("current_role", {})
    is_supplementary = name not in ORIGINAL_CANDIDATES

    with st.expander(name, expanded=False):

        meta_parts = [p for p in [role.get("employer", ""), role.get("title", "")] if p]
        if meta_parts:
            st.markdown(
                "<div class='candidate-meta'>"
                + " &nbsp;·&nbsp; ".join(html.escape(p) for p in meta_parts)
                + ("&nbsp; &nbsp;<em>(Supplementary)</em>" if is_supplementary else "")
                + "</div>",
                unsafe_allow_html=True,
            )

        section_label("Career Narrative")
        write_wrapped_text(result.get("career_narrative", ""))

        st.write("")

        section_label("Experience")
        render_tags(result.get("experience_tags", []))

        st.write("")

        section_label("Firm AUM Context")
        write_wrapped_text(result.get("firm_aum_context", ""))

        st.divider()

        mobility = result.get("mobility_signal", {})
        role_fit = result.get("role_fit", {})

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("<div class='col-title'>Mobility Signal</div>",
                        unsafe_allow_html=True)
            render_score_chip(mobility.get("score", 0), 5)
            st.markdown(
                f"<div class='col-body'>{html.escape(mobility.get('rationale', ''))}</div>",
                unsafe_allow_html=True,
            )

        with col2:
            st.markdown("<div class='col-title'>Role Fit</div>",
                        unsafe_allow_html=True)
            render_score_chip(role_fit.get("score", 0), 10)
            st.markdown(
                f"<div class='col-body'>{html.escape(role_fit.get('justification', ''))}</div>",
                unsafe_allow_html=True,
            )

        st.divider()

        section_label("Outreach Hook")
        st.info(result.get("outreach_hook", ""))


# ── Main UI ────────────────────────────────────────────────────────────────────

st.title("PPP Candidate Research Agent")
st.caption(
    "Upload a CSV of candidates to generate structured intelligence briefings "
    "for use in executive search. "
    "Candidates are processed sequentially with a short pause between each "
    "to stay within Anthropic token-per-minute limits."
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
    pipeline_running = st.session_state.get("pipeline_running", False)

    if not pipeline_running and st.button("Generate briefings", type="primary"):
        if not os.getenv("ANTHROPIC_API_KEY"):
            st.error(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to a .env file in the project root."
            )
            st.stop()

        st.session_state.pop("results", None)
        st.session_state.pop("failures", None)

        import threading
        stop_event = threading.Event()
        shared: dict[str, Any] = {
            "statuses": ["Waiting"] * len(candidates),
            "results": [],
            "failures": [],
            "done": False,
        }
        thread = threading.Thread(
            target=_pipeline_worker,
            args=(candidates, shared, stop_event),
            daemon=True,
        )

        st.session_state.pipeline_running = True
        st.session_state.pipeline_shared = shared
        st.session_state.pipeline_stop_event = stop_event
        st.session_state.pipeline_thread = thread
        st.session_state.pipeline_candidates = candidates

        thread.start()
        st.rerun()

# ── Pipeline progress UI (shown while running) ─────────────────────────────────
if st.session_state.get("pipeline_running", False):
    _candidates = st.session_state.pipeline_candidates
    shared = st.session_state.pipeline_shared
    stop_event = st.session_state.pipeline_stop_event
    thread = st.session_state.pipeline_thread

    st.subheader("Progress")
    st.session_state.status_placeholder = st.empty()
    with st.session_state.status_placeholder.container():
        _render_status_table(_candidates, shared["statuses"])

    if st.button("Stop", type="secondary"):
        stop_event.set()
        for idx, s in enumerate(shared["statuses"]):
            if s == "Waiting":
                shared["statuses"][idx] = "Stopped"

    if shared.get("done") or not thread.is_alive():
        st.session_state.results = shared["results"]
        st.session_state.failures = shared["failures"]
        st.session_state.pipeline_running = False
        for k in ("pipeline_shared", "pipeline_stop_event", "pipeline_thread", "pipeline_candidates"):
            st.session_state.pop(k, None)
        st.rerun()
    else:
        time.sleep(1)
        st.rerun()

# ── Results — rendered from session_state so they survive download reruns ──────
if "results" in st.session_state:
    results = st.session_state.results
    failures = st.session_state.failures

    st.success(f"Complete: {len(results)} succeeded, {len(failures)} failed.")

    if failures:
        st.subheader("Failures")
        for name, reason in failures:
            st.warning(f"**{name}**: {reason[:800]}")

    if results:
        st.subheader("Candidate Briefings")
        for result in results:
            _render_result_card(result)

    output_path = Path(__file__).parent / "output" / "output.json"
    if output_path.exists():
        st.divider()
        with open(output_path, "r", encoding="utf-8") as f:
            output_json = f.read()
        st.download_button(
            label="Download output.json",
            data=output_json,
            file_name="output.json",
            mime="application/json",
            type="primary",
        )
