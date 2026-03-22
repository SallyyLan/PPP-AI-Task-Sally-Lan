# Design note — PPP Candidate Research Agent

## 1. Architecture choices and why

**Workflow as a document, not as code (`workflows/research_phase.md`)**
PPP's research rules — query discipline, search budget, discovery tiers, confidence vocabulary — live in a prose document that a consultant or researcher can read, critique, and edit without touching Python. The agent receives it as system context on every call. This means prompt improvements (tighter query rules, revised scoring rubrics, new confidence tiers) ship without a code change. It also makes the system auditable: "what did the agent know?" is answered by reading one human-readable file, not reverse-engineering a function.

**Three-stage pipeline: Phase 0 → Phase 1 → Phase 2**
- *Phase 0 (pre-flight)* uses a small tool budget — at most 2 web searches plus an optional `fetch_url` — to confirm whether the CSV row matches reality before committing the full research budget. In the initial test run, four of five candidates had wrong employer or title data. Without a pre-flight gate, each bad row would burn the full 5-search Phase 1 budget and produce a confidently wrong briefing. Phase 0 logs a verdict (`CONFIRMED`, `MISMATCH`, `NOT_FOUND`) and passes correction context to Phase 1.
- *Phase 1 (research memo)* is deliberately unstructured: the model writes in prose with explicit confidence labels and is not trying to produce JSON. Keeping format concerns out of this phase means the model can focus entirely on what is and isn't verified.
- *Phase 2 (synthesis)* is a fresh completion that forces a single `create_briefing` tool call. The model only maps already-gathered evidence into fixed schema fields — it does not re-discover facts. This eliminates brittle JSON extraction from free text and cuts hallucination in structured output.

**`fetch_url` as client-side, deterministic code**
Anthropic's `web_search` handles discovery. Direct page fetch handles firm team listings and ASX-style announcements that search snippets distort or omit. Implementing `fetch_url` in code (rather than as a prompt instruction) gives testable, predictable behaviour for HTTP, truncation, and HTML stripping.

**Pydantic as the single schema contract**
One module (`schema.py`) defines what "valid" means for consultants, downstream systems, and tests. Validation runs immediately before every append to `output/output.json`, so bad rows surface as agent retries or explicit error objects — never as silent schema drift.

---

## 2. What I would do differently with more time

- **Structured source ledger.** Require Phase 1 to emit source objects (URL, quoted span, which field it supports) alongside prose, and render them in the UI as clickable citations. A consultant should be able to verify a `[VERIFIED]` claim in seconds, not by re-running the search.
- **Evaluation harness.** Build 15–20 anonymised golden profiles and score every prompt or model change against them on factual precision, rubric adherence, and honest-uncertainty rate. Without this, prompt tuning is anecdotal.
- **Entity normalisation.** Resolve employer names once per run (Pendal/Perpetual, subsidiaries, trading names) and cache firm-level AUM and context across candidates at the same house. This removes contradictions between briefings and saves two to three searches per duplicate firm.
- **Parallelism — why it doesn't work yet, and what would fix it.** Candidates are processed strictly sequentially with a 10-second inter-candidate pause. Parallel processing is not viable at current Anthropic API quotas: each candidate pipeline (Phase 0 + Phase 1 + Phase 2) consumes roughly 15,000–50,000 tokens; running two or more candidates simultaneously exhausts the TPM window within minutes and triggers 429 rate errors. Worse, retries from multiple concurrent threads arrive simultaneously, amplifying the problem rather than resolving it. The fix requires a token-count–aware job queue that reads actual TPM consumption from API response headers and spaces new submissions in real time — not a fixed sleep, which is both too slow for small batches and too fast under heavy load.
- **Tenure extraction in code.** Parse verified start-date strings from Phase 1 and derive `tenure_years` with explicit rounding rules, rather than trusting the model's integer. This eliminates a quiet source of off-by-one errors.

---

## 3. One additional automation I would build for PPP

**Mandate-aware brief generation with CRM push.**

Today the target role and scoring rubric are hard-coded for a single mandate. PPP runs multiple concurrent searches with different role titles, AUM bands, geographies, and seniority requirements.

The automation: when a consultant creates or updates a mandate record in the CRM, a webhook fires the pipeline with mandate parameters injected into the workflow document — replacing the single hard-coded target role with the actual brief. The agent scores role fit against *that* mandate's criteria. Completed briefings, with source ledger, confidence summary, and a data-quality flag, write back to each candidate record in the CRM. A Slack digest notifies the lead consultant each morning, ranked by role-fit score and research completeness, so the first thing they see is a triage-ready stack rather than a raw list.

The goal is not to replace consultant judgment. It is to ensure that judgment is applied to evidence — not to the task of assembling it.
