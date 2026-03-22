# PPP Candidate Research Agent

A WAT-framework (Workflow + Agent + Tools) agent system built for Platinum Pacific Partners (PPP). A non-technical consultant uploads a CSV of candidates; the agent reads a workflow SOP, reasons autonomously about what to find, calls deterministic research tools in the order it decides, handles uncertainty honestly, and produces a validated `output/output.json` plus a readable Streamlit UI.

---

## Setup (under 5 minutes)

```bash
# 1. Clone the repository
git clone <repo-url>
cd ppp-candidate-agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure API key
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# 4. Confirm the candidate data is present
ls data/candidates.csv
```

---

## How to Run

### CLI

```bash
python run.py data/candidates.csv
```

Processes all candidates, prints live status, writes results to `output/output.json`.

### Streamlit UI

```bash
streamlit run app.py
```

Open the URL shown in the terminal (default: `http://localhost:8501`). Upload a CSV, click **Generate briefings**, watch the live progress table, then download `output.json`.

---

## Output

`output/output.json` — a JSON object with a `candidates` array. Each entry is a validated briefing conforming to the Section 4 schema:

```json
{
  "candidates": [
    {
      "candidate_id": "candidate_1",
      "full_name": "...",
      "current_role": { "title": "...", "employer": "...", "tenure_years": 3.5 },
      "career_narrative": "3–4 sentence career story.",
      "experience_tags": ["tag1", "tag2"],
      "firm_aum_context": "...",
      "mobility_signal": { "score": 3, "rationale": "..." },
      "role_fit": {
        "role": "Head of Distribution / National BDM",
        "score": 8,
        "justification": "..."
      },
      "outreach_hook": "One sentence hook for opening a call."
    }
  ]
}
```

A sample run result is committed to this repository at `output/output.json`.

To programmatically validate:

```bash
python -c "from schema import OutputFile; import json; OutputFile.model_validate(json.load(open('output/output.json'))); print('Schema valid')"
```

---

## About the Candidates

The original 5 candidates from Appendix A of the brief are included as provided (rows 1–5 in `data/candidates.csv`). The submission uses the original 5 candidates from Appendix A without supplementation.

---

## Architecture

Three WAT layers:

| Layer | File | Role |
|-------|------|------|
| **Workflow** | `workflows/research_phase.md` | Agent SOP — objective, tool decision guidance, confidence labelling rules, scoring rubrics, role spec, edge cases. Written in plain English. A non-technical PPP team member can read, understand, and improve it without touching code. |
| **Agent** | `agent.py` | Reads the workflow SOP as its system prompt. Runs a three-phase pipeline: Phase 0 pre-flight (identity check), Phase 1 research (up to 5 web searches), Phase 2 synthesis (forced structured tool call). Self-corrects on validation failure. Uses `claude-sonnet-4-5`. |
| **Tools** | `tools/write_output.py` + `agent.py` | `write_output` (thread-safe atomic append to `output.json`); `fetch_url` (client-side HTTP page fetch, implemented in `agent.py`); `web_search` (Anthropic server-side tool, no local code). They execute; they do not reason. |

The orchestrator (`run.py`) iterates candidates and isolates failures. The frontend (`app.py`) provides the non-technical UI. The schema (`schema.py`) is the single source of truth imported by everything.

---

## Running Tests

```bash
pytest tests/ -v
```

Unit tests covering all developer tools including edge cases. All must pass before running the full pipeline.

---

## Known Limitations

- **LinkedIn scraping is not supported** — bot detection blocks all requests. The agent short-circuits LinkedIn URLs immediately and falls back to web search, as designed.
- **AUM figures for smaller or recently merged firms may be estimated** — all estimates are flagged inline with `[ESTIMATED — {basis}]` so consultants can judge confidence.
- **Web search may return sparse results for less prominent names** — the agent handles this by flagging confidence rather than fabricating data.
- **Pendal Group / Perpetual merger** — the agent is instructed in the SOP to search both names and note the merger context.

---

## See Also

`design_note.md` — Architecture rationale, what I'd build next, and a PPP-specific automation idea.
