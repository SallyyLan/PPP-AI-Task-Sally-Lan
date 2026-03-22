"""
agent.py — Two-phase Claude pipeline for PPP candidate research.

Phase 1 (Research): Claude runs an agentic loop with the web_search tool,
gathering candidate data and producing a plain-text research summary.

Phase 2 (Synthesis): A second Claude call uses a forced create_briefing tool call
(structured output) to produce the final CandidateBriefing JSON directly from the
research summary — no regex JSON extraction, no in-loop schema validation.

Key improvements over v1:
  - Phase 1 loop body and exit condition are clearly separated into distinct
    handler functions; the main loop only decides what to do next.
  - web_search quality: a query-construction preamble is injected into the
    initial user message, enforcing the 3-6 token / dual-anchor / no-NL rules
    from research_phase.md Section 3.
  - research_text passed to Phase 2 is no longer hard-truncated at 8,000 chars.
    Instead it is compressed by a lightweight structured extractor that preserves
    every [VERIFIED/ESTIMATED/UNVERIFIED] claim while staying within a generous
    token budget.  If compression is not needed the full text is passed.
  - Search-budget verification: Phase 1 logs a warning when fewer than 5 searches
    are used, so operators can detect systematic early-exit behaviour.
  - pause_turn handling is explicit: each pause appends the full assistant content
    and a targeted continuation prompt rather than a bare "Please continue."
  - Retry logic distinguishes TPD (tokens-per-day) errors, which are not worth
    retrying with exponential backoff, from transient TPM/RPM errors.
  - All magic numbers are named constants at the top of the file.
  - Token logging now includes the phase-level research_text character count so
    compression ratio can be audited.
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Model ──────────────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-5"

# ── Timing constants ───────────────────────────────────────────────────────────
BETWEEN_TURNS_DELAY = 5          # seconds between pause_turn continuations
MAX_PAUSES = 3                   # maximum pause_turn continuations before giving up
MAX_RETRIES = 6                  # API call retry attempts
RETRY_BASE_DELAY = 30            # seconds; doubles on each attempt up to RETRY_MAX_DELAY
RETRY_MAX_DELAY = 120            # seconds ceiling for exponential backoff

# ── Token limits ───────────────────────────────────────────────────────────────
# Phase 0 (pre-flight): small — one search + one fetch, plain text verdict only.
PHASE0_MAX_TOKENS = 2048
# Phase 1 (research): generous — full career + firm context + assessments.
# 4096 was too low when a candidate has a rich public profile; 8192 avoids
# mid-summary pause_turn continuations and truncation of the assessments block.
PHASE1_MAX_TOKENS = 8192
# Phase 2 (synthesis): moderate — only filling a fixed JSON schema.
PHASE2_MAX_TOKENS = 4096

# ── Research-text budget for Phase 2 ──────────────────────────────────────────
# Hard token limit for the research summary passed to Phase 2.
# ~180k chars fits inside claude-sonnet-4-5's 200k context with system prompt headroom.
# Compression kicks in only when raw text exceeds RESEARCH_COMPRESS_THRESHOLD.
RESEARCH_MAX_CHARS = 60_000       # generous ceiling — well within context window
RESEARCH_COMPRESS_THRESHOLD = 40_000   # only compress if text exceeds this

# ── Paths ──────────────────────────────────────────────────────────────────────
RESEARCH_PHASE_PATH = Path(__file__).parent / "workflows" / "research_phase.md"
ERRORS_LOG = Path(__file__).parent / ".tmp" / "errors.log"
TOKENS_LOG = Path(__file__).parent / ".tmp" / "tokens.log"

# ── Phase 0 tools: 1 web search + direct URL fetch ────────────────────────────
# Phase 0 pre-flight uses a single search to confirm identity and optionally
# fetches the employer's team/leadership page directly for authoritative
# confirmation that is often not indexed by search engines.
PHASE0_TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 2,
        # No user_location for Phase 0 — identity verification is global.
        # Many employers are international firms (Fidelity International,
        # Man Group, Mercer) and AU-biased search can miss their global
        # announcements. Phase 1 uses AU location for industry-specific research.
    },
    {
        "name": "fetch_url",
        "description": (
            "Fetch the text content of a public URL directly. "
            "Use for employer team/leadership pages (e.g. 'challenger.com.au/about/our-people'), "
            "ASX announcement pages, ASIC register results, or any page whose content "
            "is not reliably indexed by search engines. "
            "Returns the page text; ignore navigation and boilerplate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to fetch (must start with https://).",
                }
            },
            "required": ["url"],
        },
    },
]

# ── Phase 1 tools: web search only ────────────────────────────────────────────
PHASE1_TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 5,
        "user_location": {
            "type": "approximate",
            "country": "AU",
            "timezone": "Australia/Sydney",
        },
    },
]

# ── Phase 1 search-quality preamble ───────────────────────────────────────────
# Injected into the initial user message so that query construction rules from
# research_phase.md Section 3 are front-of-mind at the moment of first search.
SEARCH_QUALITY_PREAMBLE = """\
Before you begin researching, note these search guidelines:
- Use keyword-style queries, not conversational sentences. \
Example: "Andrew Swan Perpetual distribution" not "Who is Andrew Swan at Perpetual".
- Every query must contain at least two identifiers: name + employer, or name + title keyword.
- After receiving results, confirm the candidate is named at the stated employer \
before recording any fact as [VERIFIED].
- If a query returns no relevant results, change the anchor entirely — do not rephrase the same query.
- Allocate your 5 searches by purpose: \
slot 1 = identity confirmation, slot 2 = tenure/title, \
slot 3 = career history, slot 4 = firm AUM, slot 5 = largest remaining gap.
- Start searching now — do not write the output format before you have run your searches.

"""

# ── Phase 2 tool: structured output schema for CandidateBriefing ──────────────
CREATE_BRIEFING_TOOL = {
    "name": "create_briefing",
    "description": (
        "Create a structured candidate briefing from research findings. "
        "Populate every field according to the schema. "
        "The role field in role_fit must be exactly 'Head of Distribution / National BDM'. "
        "Preserve [VERIFIED], [ESTIMATED], and [UNVERIFIED] labels from the research summary "
        "in the relevant text fields."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string"},
            "full_name": {"type": "string"},
            "current_role": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "employer": {"type": "string"},
                    "tenure_years": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Whole years in current role, >= 0. Round to nearest year.",
                    },
                },
                "required": ["title", "employer", "tenure_years"],
            },
            "career_narrative": {
                "type": "string",
                "description": "3 to 4 sentences summarising the candidate's career arc.",
            },
            "experience_tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "description": "At least 2 short tags describing key experience areas.",
            },
            "firm_aum_context": {"type": "string"},
            "mobility_signal": {
                "type": "object",
                "properties": {
                    "score": {"type": "integer", "minimum": 1, "maximum": 5},
                    "rationale": {
                        "type": "string",
                        "description": "1 to 2 sentences referencing specific signals.",
                    },
                },
                "required": ["score", "rationale"],
            },
            "role_fit": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["Head of Distribution / National BDM"],
                        "description": "Must be exactly this string.",
                    },
                    "score": {"type": "integer", "minimum": 1, "maximum": 10},
                    "justification": {
                        "type": "string",
                        "description": "2 to 3 sentences referencing specific criteria.",
                    },
                },
                "required": ["role", "score", "justification"],
            },
            "outreach_hook": {
                "type": "string",
                "description": "A single sentence personalised to the candidate's current situation.",
            },
        },
        "required": [
            "candidate_id",
            "full_name",
            "current_role",
            "career_narrative",
            "experience_tags",
            "firm_aum_context",
            "mobility_signal",
            "role_fit",
            "outreach_hook",
        ],
    },
}

# ── Phase 2 system prompt ──────────────────────────────────────────────────────
SYNTHESIS_SYSTEM_PROMPT = """You are a senior research analyst at Platinum Pacific Partners (PPP), \
a specialist executive search firm focused on the Australian funds management industry. \
You have been given a plain-text research summary for one candidate. \
Your task is to synthesise it into a structured candidate briefing using the create_briefing tool.

## Scoring Rubrics

### Mobility Score (1–5)
- 1 — very unlikely to move: recently started (<12 months), recently promoted, publicly committed.
- 2 — probably settled but open to exceptional opportunities: 1–3 years, positive trajectory.
- 3 — approaching a natural transition point: 3–4 years, flat trajectory, or firm in moderate flux.
- 4 — likely open: 5+ years, no recent promotion, firm undergoing ownership change or contraction.
- 5 — actively or probably seeking: very long tenure with no progression, firm in distress, \
public departure signals.

Write 1–2 sentences of rationale referencing specific signals from the research.

### Role Fit Score (1–10)
Role: Head of Distribution / National BDM at a mid-tier Australian active asset manager \
(AUM $5–20B, institutional and wholesale focus).

We are looking for:
- 10+ years in Australian funds management distribution, sales, or investor relations
- Proven track record with institutional investors, platforms, and IFAs
- Deep networks across superannuation funds, family offices, and financial planning dealer groups
- Experience managing a national sales team (3–8 direct reports)
- Strong investment product knowledge across equities, fixed income, and/or alternatives
- Existing profile and brand in the Australian wholesale and institutional market

Scoring bands:
- 9–10: currently holds an equivalent role at a directly comparable firm, all six criteria met
- 7–8: senior distribution leadership, strong network, comparable firm; minor gap in one criterion
- 5–6: senior BDM or second-in-command; good trajectory; moderate gaps in two criteria
- 3–4: relevant distribution experience but not leadership level, or different market segment
- 1–2: tangential experience only, or significant misalignment

Write 2–3 sentences of justification. Name specific strengths and gaps.

### Outreach Hook
One sentence only. Must reference something specific to this candidate's current situation \
(their firm, a recent event, their trajectory). Must not be generic. \
Example: "Given Apex's recent ownership transition, I thought it worth reaching out — \
we are working with a well-capitalised active manager looking to build out their distribution \
leadership, and your background across wholesale and platform channels is exactly the profile \
they have in mind."

## Career narrative when identity verification fails
Use this **fixed shape** whenever the research indicates the named person could not be matched to the \
stated employer or role, or the summary contains `[NOT FOUND]` for identity or current role:
- You may open with one bracketed label only, e.g. `[NOT FOUND — short reason]`.
- **After** any brackets, write **exactly three** sentences of plain prose (a fourth sentence is allowed \
if you must cite one extra verified fact, e.g. who actually holds the role). Each sentence ends with `.` \
and the next sentence **must** start with a capital letter — do not join them with semicolons or em dashes.
- Sentence 1: What the intake claimed (name, title, employer).
- Sentence 2: What public research concluded (no match, name collision, who holds the role, etc.).
- Sentence 3: Implication or next step (validate with client, pause outreach, etc.).
- Keep `[VERIFIED]` / `[UNVERIFIED]` labels on specific claims inside those sentences where the research \
supplied them.

Example `career_narrative` (same pattern; replace with this candidate's facts):
`[NOT FOUND — identity could not be verified at the stated employer after dual-anchor searches.] The intake listed this person as Head of Distribution at Example Asset Management. Public sources showed no profile matching this name in that role at that firm, and a different individual appeared in the target role [VERIFIED]. Recommend confirming employer, title, and spelling with the client before any outreach.`

## Instructions
- Preserve [VERIFIED], [ESTIMATED — {basis}], and [UNVERIFIED] labels from the research summary \
in the relevant text fields (career_narrative, firm_aum_context).
- The role field in role_fit must be exactly "Head of Distribution / National BDM".
- career_narrative must be 3–4 sentences of plain prose. Content inside \
[...] confidence labels (e.g. [NOT FOUND — ...]) does not count as a sentence — \
write exactly 3–4 standalone sentences outside of any brackets. When identity fails, follow \
**Career narrative when identity verification fails** above (three-sentence body after the label).
- outreach_hook must be a single sentence (no full stop mid-sentence). \
Content inside [...] labels does not count as an extra sentence.
- experience_tags must have at least 2 items.
- If the research summary contains [NOT FOUND] fields, reflect that uncertainty honestly \
in the relevant output fields rather than inventing data.
"""


# ── Phase 0 system prompt ─────────────────────────────────────────────────────
PHASE0_SYSTEM_PROMPT = """\
You are a research analyst verifying candidate intake data for an executive search firm.

Your ONLY task is identity verification: determine whether the named person actually holds \
the stated role at the stated employer.

Tools available:
- web_search: run keyword queries (max 2 uses). Use short anchor queries: \
  name + employer + role fragment.
- fetch_url: fetch a URL directly. Prioritise the employer's official team/leadership page \
  (e.g. "https://www.challenger.com.au/about/our-people") over search results when you need \
  to confirm who currently holds a role. Also useful for ASX announcements on listed employers.

Decision rules:
- CONFIRMED: a public source names this person at this employer in this or a closely related role.
- MISMATCH: public sources name a different person in the role, or name this person at a \
  different employer.
- NOT_FOUND: no public confirmation either way after using your budget.

Output format (plain text, no JSON):
VERDICT: CONFIRMED | MISMATCH | NOT_FOUND
EVIDENCE: one sentence citing the source and what it says.
CORRECT_EMPLOYER: (only if MISMATCH) the employer where this person was actually found.
CORRECT_TITLE: (only if MISMATCH) the title actually found.

Do not write anything else. Do not perform career research. Stop after the verdict block.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_research_system_prompt() -> str:
    """Load the Phase 1 research system prompt from file."""
    if not RESEARCH_PHASE_PATH.exists():
        raise FileNotFoundError(f"Research phase prompt not found at {RESEARCH_PHASE_PATH}")
    return RESEARCH_PHASE_PATH.read_text(encoding="utf-8")


def _interruptible_sleep(seconds: float) -> None:
    """Sleep in 0.1 s increments so the thread can receive a StopException."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(min(0.1, deadline - time.time()))


def _log_error(message: str) -> None:
    """Append a timestamped error message to .tmp/errors.log."""
    try:
        ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(ERRORS_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def _log_tokens(
    candidate_index: int,
    full_name: str,
    phase: str,
    turn: int,
    input_tokens: int,
    output_tokens: int,
    extra: str = "",
) -> None:
    """Append per-call token usage to .tmp/tokens.log."""
    try:
        TOKENS_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        suffix = f"  {extra}" if extra else ""
        with open(TOKENS_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"[{ts}] candidate_{candidate_index} ({full_name}) | {phase} turn {turn} | "
                f"in={input_tokens:,}  out={output_tokens:,}  "
                f"total={input_tokens + output_tokens:,}{suffix}\n"
            )
    except Exception:
        pass


def _is_non_retryable(exc: anthropic.RateLimitError) -> bool:
    """
    Return True for daily quota errors (TPD / RPD) which will not resolve within
    the retry window and should fail fast rather than burning the delay budget.
    """
    msg = str(exc).lower()
    return "tokens per day" in msg or "tpd" in msg or "requests per day" in msg or "rpd" in msg


def _classify_rate_limit(exc: anthropic.RateLimitError) -> str:
    msg = str(exc).lower()
    if "tokens per minute" in msg or "tpm" in msg:
        return "rate limit [tokens/min]"
    if "tokens per day" in msg or "tpd" in msg:
        return "rate limit [tokens/day — daily quota exhausted]"
    if "requests per minute" in msg or "rpm" in msg:
        return "rate limit [requests/min]"
    if "requests per day" in msg or "rpd" in msg:
        return "rate limit [requests/day — daily quota exhausted]"
    return "rate limit"


def _call_with_retry(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    tools: list,
    messages: list,
    candidate_index: int,
    full_name: str,
    max_retries: int = MAX_RETRIES,
    tool_choice: dict | None = None,
    phase: str = "?",
    turn: int = 0,
    log_extra: str = "",
    max_tokens: int = PHASE1_MAX_TOKENS,
) -> anthropic.types.Message | None:
    """
    Call client.messages.create with exponential backoff on transient errors.

    Daily quota errors (TPD/RPD) are not retried — they fail immediately.
    Returns None if all retries are exhausted or a non-retryable error occurs.
    """
    delay = RETRY_BASE_DELAY
    for attempt in range(max_retries):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "tools": tools,
                "messages": messages,
            }
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            resp = client.messages.create(**kwargs)
            _log_tokens(
                candidate_index, full_name, phase, turn,
                resp.usage.input_tokens, resp.usage.output_tokens,
                extra=log_extra,
            )
            return resp

        except anthropic.RateLimitError as e:
            error_kind = _classify_rate_limit(e)
            if _is_non_retryable(e):
                _log_error(
                    f"candidate_{candidate_index} ({full_name}): {error_kind} — "
                    f"not retrying (daily quota exhausted)"
                )
                return None
            if attempt == max_retries - 1:
                _log_error(
                    f"candidate_{candidate_index} ({full_name}): {error_kind} "
                    f"after {max_retries} retries — giving up"
                )
                return None
            jittered = delay * random.uniform(0.5, 1.5)
            _log_error(
                f"candidate_{candidate_index} ({full_name}): {error_kind}, "
                f"retrying in {jittered:.0f}s (attempt {attempt + 1}/{max_retries})"
            )
            _interruptible_sleep(jittered)
            delay = min(delay * 2, RETRY_MAX_DELAY)

        except anthropic.InternalServerError as e:
            if attempt == max_retries - 1:
                _log_error(
                    f"candidate_{candidate_index} ({full_name}): server error after "
                    f"{max_retries} retries: {e}"
                )
                return None
            jittered = delay * random.uniform(0.5, 1.5)
            _log_error(
                f"candidate_{candidate_index} ({full_name}): server error, "
                f"retrying in {jittered:.0f}s (attempt {attempt + 1}/{max_retries}): {e}"
            )
            _interruptible_sleep(jittered)
            delay = min(delay * 2, RETRY_MAX_DELAY)

        except anthropic.APIError as e:
            _log_error(f"candidate_{candidate_index} ({full_name}): non-retryable API error: {e}")
            return None

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — research loop (body and exit condition separated)
# ─────────────────────────────────────────────────────────────────────────────

def _collect_text_blocks(content: list) -> list[str]:
    """Extract all non-empty text strings from a response content list."""
    return [block.text for block in content if hasattr(block, "text") and block.text]


def _count_search_calls(messages: list[dict]) -> int:
    """
    Count how many web_search tool blocks appear across all assistant turns
    in the accumulated message history.  Used to audit search budget usage.

    Matches by name only (not type) so that server-side pause_turn blocks —
    which may have a type other than "tool_use" — are still counted.
    """
    count = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                # SDK object: match by name regardless of block type
                if hasattr(block, "name") and block.name == "web_search":
                    count += 1
                # Plain dict (e.g. after JSON round-trip)
                elif isinstance(block, dict) and block.get("name") == "web_search":
                    count += 1
    return count


def _handle_pause_turn(
    response: anthropic.types.Message,
    research_parts: list[str],
    messages: list[dict],
    pause_count: int,
    candidate_index: int,
    full_name: str,
) -> tuple[list[str], list[dict], int, bool]:
    """
    Process a pause_turn response.

    Returns (updated_parts, updated_messages, new_pause_count, should_break).
    should_break=True when the pause limit is reached.
    """
    new_text = _collect_text_blocks(response.content)
    updated_parts = research_parts + new_text
    pause_count += 1

    if pause_count > MAX_PAUSES:
        _log_error(
            f"candidate_{candidate_index} ({full_name}): "
            f"pause_turn limit ({MAX_PAUSES}) reached — proceeding with partial research"
        )
        return updated_parts, messages, pause_count, True

    # Append the full assistant turn so the model retains context of what it
    # has already written and what searches it has already run.
    updated_messages = messages + [
        {"role": "assistant", "content": response.content},
        {
            "role": "user",
            "content": (
                "Continue the research from where you left off. "
                "Check your remaining search budget before issuing the next query. "
                "If all 5 searches are exhausted, proceed directly to the output format."
            ),
        },
    ]
    _interruptible_sleep(BETWEEN_TURNS_DELAY)
    return updated_parts, updated_messages, pause_count, False


# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 — pre-flight identity check
# ─────────────────────────────────────────────────────────────────────────────

def _handle_fetch_url(tool_use_block: Any) -> dict[str, Any]:
    """
    Execute a fetch_url tool call and return a tool_result dict.
    Uses urllib to retrieve the page text; strips HTML tags for readability.
    Returns an error message in the content if the fetch fails.
    """
    import re
    import urllib.request
    import urllib.error

    url = tool_use_block.input.get("url", "")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PPP-research-agent/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        # Strip HTML tags and collapse whitespace
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s{3,}", "\n\n", text).strip()
        # Cap at 8k chars to stay within token budget
        if len(text) > 8000:
            text = text[:8000] + "\n\n[... page truncated at 8,000 chars ...]"
        content = text if text else "[empty page]"
    except urllib.error.HTTPError as e:
        content = f"[HTTP {e.code} fetching {url}]"
    except Exception as e:
        content = f"[fetch failed: {e}]"

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_block.id,
        "content": content,
    }


def _is_real_linkedin_url(url: str) -> bool:
    """
    Return True only if url looks like a genuine LinkedIn profile slug.

    Rejects empty strings, bare domains, and known placeholder patterns
    such as 'linkedin.com/in/search' that the CSV template uses.
    """
    if not url or not url.strip():
        return False
    raw = url.strip()
    # Accept with or without scheme
    normalized = raw if raw.startswith("http") else "https://" + raw
    if "linkedin.com/in/" not in normalized:
        return False
    slug = normalized.split("linkedin.com/in/")[-1].strip("/").split("?")[0]
    # Reject blank or known placeholder slugs
    placeholder_slugs = {"search", "in", "pub", "profile", "me"}
    return bool(slug) and slug not in placeholder_slugs and len(slug) > 3


def _normalise_linkedin_url(url: str) -> str:
    """Return a fully qualified https:// LinkedIn URL."""
    url = url.strip()
    return url if url.startswith("http") else "https://" + url


def _run_phase0_preflight(
    client: anthropic.Anthropic,
    candidate: dict[str, Any],
    candidate_index: int,
) -> dict[str, str]:
    """
    Phase 0: cheap identity pre-flight check (max 2 searches + fetch_url).

    Returns a dict with:
      "verdict"          : "CONFIRMED" | "MISMATCH" | "NOT_FOUND"
      "evidence"         : one-sentence source citation
      "correct_employer" : corrected employer name (MISMATCH only, else "")
      "correct_title"    : corrected title (MISMATCH only, else "")
    """
    full_name = candidate.get("full_name", "Unknown")
    employer = candidate.get("current_employer", "")
    title = candidate.get("current_title", "")
    raw_linkedin = candidate.get("linkedin_url", "") or ""

    # Only use the LinkedIn URL if it resolves to a real profile page.
    linkedin_url = (
        _normalise_linkedin_url(raw_linkedin)
        if _is_real_linkedin_url(raw_linkedin)
        else None
    )

    # Build the step list dynamically so LinkedIn only appears when available.
    steps = [
        f'1. Run one web_search: "{full_name}" "{employer}" — short anchor query only.',
    ]
    if linkedin_url:
        steps.append(
            f"2. Fetch the LinkedIn profile URL provided: {linkedin_url} — "
            f"this is a real profile URL. Use it to confirm name, current employer, and title."
        )
        steps.append(
            "3. If identity is still uncertain after the search and LinkedIn fetch, "
            "fetch the employer's official team or leadership page to check whether "
            "this person is listed."
        )
        steps.append("4. Output your VERDICT block and stop.")
    else:
        steps.append(
            "2. If identity is uncertain after the search, fetch the employer's official "
            f"team or leadership page (e.g. https://www.{employer.lower().replace(' ', '')}.com.au/about/our-people) "
            "to check whether this person is listed."
        )
        steps.append("3. Output your VERDICT block and stop.")

    user_message = (
        f"Verify whether this person currently holds the stated role at the stated employer.\n\n"
        f"Name: {full_name}\n"
        f"Stated employer: {employer}\n"
        f"Stated title: {title}\n"
        + (f"LinkedIn URL: {linkedin_url}\n" if linkedin_url else "(no LinkedIn URL provided)\n")
        + f"\nSteps:\n"
        + "\n".join(steps)
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    turn = 0

    # Phase 0 runs its own mini tool-use loop (max 5 turns to avoid runaway).
    for _ in range(5):
        turn += 1
        response = _call_with_retry(
            client, MODEL, PHASE0_SYSTEM_PROMPT, PHASE0_TOOLS, messages,
            candidate_index, full_name,
            tool_choice={"type": "auto"},
            phase="phase0", turn=turn,
            max_tokens=PHASE0_MAX_TOKENS,
        )
        if response is None:
            break

        stop_reason = response.stop_reason

        # Collect any tool calls and execute fetch_url client-side.
        tool_results = []
        for block in response.content:
            if hasattr(block, "type") and block.type == "tool_use" and block.name == "fetch_url":
                tool_results.append(_handle_fetch_url(block))

        if stop_reason == "end_turn" or stop_reason == "pause_turn":
            # Append assistant turn, then tool results if any, then check for verdict.
            messages.append({"role": "assistant", "content": response.content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                # Continue so the model can read fetch results and produce verdict.
                if stop_reason == "pause_turn":
                    continue
                # end_turn with fetch results pending — ask for verdict.
                messages.append({
                    "role": "user",
                    "content": "You have the fetch results above. Now output your VERDICT block.",
                })
                continue

            # No pending tool results — parse verdict from response text.
            full_text = " ".join(
                b.text for b in response.content if hasattr(b, "text") and b.text
            )
            break

        if stop_reason == "tool_use":
            # Client-side tool_use (fetch_url only — web_search is server-side).
            messages.append({"role": "assistant", "content": response.content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        # Any other stop_reason — collect text and exit.
        full_text = " ".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        )
        break
    else:
        full_text = ""

    # Parse the VERDICT block from whatever text was produced.
    result = {"verdict": "NOT_FOUND", "evidence": "", "correct_employer": "", "correct_title": ""}
    for line in full_text.splitlines():
        line = line.strip()
        if line.startswith("VERDICT:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("CONFIRMED", "MISMATCH", "NOT_FOUND"):
                result["verdict"] = val
        elif line.startswith("EVIDENCE:"):
            result["evidence"] = line.split(":", 1)[1].strip()
        elif line.startswith("CORRECT_EMPLOYER:"):
            result["correct_employer"] = line.split(":", 1)[1].strip()
        elif line.startswith("CORRECT_TITLE:"):
            result["correct_title"] = line.split(":", 1)[1].strip()

    _log_error(
        f"candidate_{candidate_index} ({full_name}): "
        f"Phase 0 verdict={result['verdict']} — {result['evidence']}"
    )
    return result


def _run_phase1_research(
    client: anthropic.Anthropic,
    candidate: dict[str, Any],
    candidate_index: int,
    system_prompt: str,
) -> str | None:
    """
    Phase 1: Agentic research loop using web_search only.

    Loop structure:
      - Loop body  : collect text blocks from the latest response.
      - Exit check : inspect stop_reason and decide whether to break or continue.

    These two responsibilities are handled by dedicated functions (_handle_end_turn,
    _handle_pause_turn) so the main while-loop remains a thin dispatcher.

    Returns all accumulated assistant text as a single research document,
    or None if the API call fails entirely.
    """
    full_name = candidate.get("full_name", "Unknown")

    # Build the initial user message.
    # IMPORTANT: the user message must not reference the output format —
    # doing so causes Claude to skip straight to writing the summary without searching.
    # The instruction is split: FIRST search (explicit step 1), THEN write (step 2).
    initial_message = (
        SEARCH_QUALITY_PREAMBLE
        + f"Your task has two steps. Do them in order — do not skip step 1.\n\n"
        f"STEP 1 — SEARCH (do this first, before writing anything):\n"
        f"Use all 5 of your web_search slots to research this candidate. "
        f"Follow the discovery strategy in your workflow instructions. "
        f"Do not produce any output until you have completed your searches.\n\n"
        f"STEP 2 — WRITE (only after all searches are done):\n"
        f"Produce the plain-text research summary using the output format "
        f"in Section 9 of your workflow instructions.\n\n"
        f"Candidate number: {candidate_index}. "
        f"Use candidate_id: \"candidate_{candidate_index}\".\n"
        f"Candidate data: {json.dumps(candidate, ensure_ascii=False)}"
    )

    messages: list[dict] = [{"role": "user", "content": initial_message}]
    research_parts: list[str] = []
    pause_count = 0
    turn = 0
    # Allow one correction if Claude attempts end_turn without having searched.
    search_correction_sent = False

    while True:
        turn += 1
        response = _call_with_retry(
            client, MODEL, system_prompt, PHASE1_TOOLS, messages,
            candidate_index, full_name,
            # tool_choice="auto" keeps decision with Claude but signals tool use
            # is available and appropriate — prevents silent skip-to-output.
            tool_choice={"type": "auto"},
            phase="phase1", turn=turn,
            max_tokens=PHASE1_MAX_TOKENS,
        )
        if response is None:
            return None

        stop_reason = response.stop_reason

        # ── Exit condition dispatcher ──────────────────────────────────────────
        if stop_reason == "end_turn":
            new_text = _collect_text_blocks(response.content)

            # Count searches in the current response too — the server-side
            # web_search_20250305 tool can return search blocks AND the final
            # text summary in a single end_turn response, so we must check
            # response.content as well as the accumulated messages history.
            current_response_searches = sum(
                1 for b in response.content
                if (hasattr(b, "name") and b.name == "web_search")
                or (isinstance(b, dict) and b.get("name") == "web_search")
            )
            total_searches = _count_search_calls(messages) + current_response_searches

            # If Claude wrote the output without using any searches, push back
            # once with an explicit correction before accepting the result.
            if not search_correction_sent and total_searches == 0:
                search_correction_sent = True
                _log_error(
                    f"candidate_{candidate_index} ({full_name}): "
                    f"Claude attempted end_turn with 0 searches — sending correction"
                )
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": (
                        "You have not used any web_search slots yet. "
                        "You MUST call web_search before writing the output. "
                        "Start now with slot 1: identity confirmation — "
                        "search for the candidate by name and employer. "
                        "Do not write the output summary until all searches are done."
                    ),
                })
                continue

            research_parts.extend(new_text)
            break

        if stop_reason == "pause_turn":
            research_parts, messages, pause_count, should_break = _handle_pause_turn(
                response, research_parts, messages, pause_count,
                candidate_index, full_name,
            )
            if should_break:
                break
            continue

        # stop_reason == "tool_use": the tool must be executed client-side.
        # web_search_20250305 is server-side, so this should not normally occur,
        # but handle it explicitly rather than falling through to the error case.
        if stop_reason == "tool_use":
            _log_error(
                f"candidate_{candidate_index} ({full_name}): "
                f"unexpected stop_reason='tool_use' on turn {turn} — "
                f"web_search_20250305 is server-side; check tool configuration"
            )
            new_text = _collect_text_blocks(response.content)
            research_parts.extend(new_text)
            break

        # Any other stop reason (e.g. "max_tokens", "stop_sequence") — collect
        # whatever text was produced and exit gracefully.
        new_text = _collect_text_blocks(response.content)
        research_parts.extend(new_text)
        _log_error(
            f"candidate_{candidate_index} ({full_name}): "
            f"unexpected stop_reason='{stop_reason}' on turn {turn} — exiting loop"
        )
        break

    # Audit search budget usage so operators can detect systematic under-use.
    # The final end_turn response is never appended to messages (loop breaks first),
    # so count web_search blocks from the last response separately.
    # Match by name only (not type) to catch server-side pause_turn blocks.
    searches_in_history = _count_search_calls(messages)
    searches_in_final = sum(
        1 for block in (response.content if response is not None else [])
        if (hasattr(block, "name") and block.name == "web_search")
        or (isinstance(block, dict) and block.get("name") == "web_search")
    )
    searches_used = searches_in_history + searches_in_final
    if searches_used < 5:
        _log_error(
            f"candidate_{candidate_index} ({full_name}): "
            f"only {searches_used}/5 web searches used — check research quality"
        )

    research_text = "\n\n".join(research_parts).strip()
    return research_text if research_text else None


# ─────────────────────────────────────────────────────────────────────────────
# research_text compression for Phase 2
# ─────────────────────────────────────────────────────────────────────────────

def _compress_research_text(
    client: anthropic.Anthropic,
    raw_text: str,
    candidate_index: int,
    full_name: str,
) -> str:
    """
    When raw_text exceeds RESEARCH_COMPRESS_THRESHOLD characters, run a lightweight
    single-turn Claude call that produces a dense structured summary preserving every
    [VERIFIED], [ESTIMATED], and [UNVERIFIED] claim.

    The compressed output keeps all labelled facts and discards filler prose,
    so Phase 2 receives a lossless-in-facts summary rather than a tail-truncated slice.

    Falls back to tail-safe truncation if the compression call itself fails.
    """
    compress_system = (
        "You are a research editor. You will receive a plain-text candidate research document. "
        "Produce a dense structured summary that:\n"
        "  1. Keeps EVERY fact tagged [VERIFIED], [ESTIMATED], or [UNVERIFIED] — verbatim, tag included.\n"
        "  2. Keeps ALL scoring assessments (Mobility score, Role fit score, Outreach hook) verbatim.\n"
        "  3. Discards transitional prose, repeated context, and search methodology commentary.\n"
        "  4. Preserves the original section headings (CURRENT ROLE, CAREER HISTORY, etc.).\n"
        "  5. Outputs plain text only — no markdown, no commentary.\n"
        "The summary must be shorter than the original while containing all labelled facts."
    )

    compress_messages = [
        {
            "role": "user",
            "content": (
                f"Compress the following research document. "
                f"Preserve every labelled fact and every scoring assessment verbatim.\n\n"
                f"{raw_text}"
            ),
        }
    ]

    response = _call_with_retry(
        client, MODEL,
        system=compress_system,
        tools=[],   # no tools needed for compression
        messages=compress_messages,
        candidate_index=candidate_index,
        full_name=full_name,
        phase="compress",
        turn=1,
        log_extra=f"raw={len(raw_text):,}chars",
    )

    if response is None:
        # Compression call failed — fall back to a tail-safe truncation strategy.
        # We take the FIRST half and the LAST quarter of the text so that both
        # the opening identity section and the closing assessments are preserved.
        _log_error(
            f"candidate_{candidate_index} ({full_name}): "
            f"compression call failed — applying tail-safe truncation"
        )
        return _tail_safe_truncate(raw_text, RESEARCH_MAX_CHARS)

    compressed = "\n\n".join(
        block.text for block in response.content
        if hasattr(block, "text") and block.text
    ).strip()

    if not compressed:
        return _tail_safe_truncate(raw_text, RESEARCH_MAX_CHARS)

    _log_error(
        f"candidate_{candidate_index} ({full_name}): "
        f"research_text compressed {len(raw_text):,} → {len(compressed):,} chars"
    )
    return compressed


def _tail_safe_truncate(text: str, max_chars: int) -> str:
    """
    Truncate text to max_chars while preserving both the opening section
    (identity / current role) and the closing section (assessments / hook).

    Strategy: take the first 60% of the budget from the head, and the last 40%
    from the tail.  This ensures the PRELIMINARY ASSESSMENTS block — which is
    always at the end of the Phase 1 output — survives truncation.
    """
    if len(text) <= max_chars:
        return text
    head_budget = int(max_chars * 0.60)
    tail_budget = max_chars - head_budget
    head = text[:head_budget]
    tail = text[-tail_budget:]
    return head + "\n\n[... content truncated for length ...]\n\n" + tail


def _prepare_research_for_phase2(
    client: anthropic.Anthropic,
    raw_text: str,
    candidate_index: int,
    full_name: str,
) -> str:
    """
    Return a version of raw_text that is safe to pass to Phase 2.

    - If within RESEARCH_COMPRESS_THRESHOLD: pass as-is.
    - If between threshold and RESEARCH_MAX_CHARS: tail-safe truncate only.
    - If above RESEARCH_MAX_CHARS: compress via a Claude call, then tail-safe
      truncate if the compressed version is still over the ceiling.
    """
    char_count = len(raw_text)

    if char_count <= RESEARCH_COMPRESS_THRESHOLD:
        return raw_text

    if char_count <= RESEARCH_MAX_CHARS:
        # Trim only the middle; head and tail are both preserved.
        return _tail_safe_truncate(raw_text, RESEARCH_MAX_CHARS)

    # Above the hard ceiling — compress first.
    compressed = _compress_research_text(client, raw_text, candidate_index, full_name)
    # Guard: if compression produced something larger than the ceiling (unlikely),
    # apply tail-safe truncation as a final safety net.
    return _tail_safe_truncate(compressed, RESEARCH_MAX_CHARS)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — structured synthesis
# ─────────────────────────────────────────────────────────────────────────────

def _run_phase2_synthesis(
    client: anthropic.Anthropic,
    candidate: dict[str, Any],
    candidate_index: int,
    research_text: str,
    system_prompt: str,
) -> dict[str, Any] | None:
    """
    Phase 2: Structured output synthesis via forced create_briefing tool call.

    Takes the (possibly compressed) Phase 1 research text and produces a
    validated CandidateBriefing dict.  Uses tool_choice to guarantee a
    structured JSON response — no text parsing needed.

    Returns None on unrecoverable failure.
    """
    from pydantic import ValidationError
    from schema import CandidateBriefing

    full_name = candidate.get("full_name", "Unknown")

    user_message = (
        f"Synthesise the following research into a complete candidate briefing "
        f"using the create_briefing tool.\n\n"
        f"candidate_id: \"candidate_{candidate_index}\"\n\n"
        f"RESEARCH FINDINGS:\n{research_text}"
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]

    response = _call_with_retry(
        client, MODEL, system_prompt, [CREATE_BRIEFING_TOOL], messages,
        candidate_index, full_name,
        tool_choice={"type": "tool", "name": "create_briefing"},
        phase="phase2", turn=1,
        log_extra=f"research={len(research_text):,}chars",
        max_tokens=PHASE2_MAX_TOKENS,
    )
    if response is None:
        return None

    tool_block = next(
        (b for b in response.content if b.type == "tool_use" and b.name == "create_briefing"),
        None,
    )
    if tool_block is None:
        _log_error(
            f"candidate_{candidate_index} ({full_name}): "
            f"Phase 2 response missing create_briefing tool call "
            f"(stop_reason={response.stop_reason})"
        )
        return None

    data = tool_block.input  # dict — no JSON parsing needed

    # Coerce tenure_years to int before Pydantic sees it.
    # The model occasionally returns 2.0 instead of 2 even with an integer schema.
    try:
        if "current_role" in data and "tenure_years" in data["current_role"]:
            data["current_role"]["tenure_years"] = int(
                round(float(data["current_role"]["tenure_years"]))
            )
    except (TypeError, ValueError):
        pass  # leave as-is; Pydantic will catch it and trigger the retry

    # ── Pydantic validation with one correction retry ──────────────────────────
    try:
        briefing = CandidateBriefing.model_validate(data)
        validated = briefing.model_dump()
    except ValidationError as e:
        errors = [
            str(err["msg"]) + f" (field: {'.'.join(str(x) for x in err['loc'])})"
            for err in e.errors()
        ]
        _log_error(
            f"candidate_{candidate_index} ({full_name}): Phase 2 Pydantic validation failed, "
            f"retrying with errors: {errors}"
        )

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": json.dumps({"validation_errors": errors}),
            }],
        })

        retry_response = _call_with_retry(
            client, MODEL, system_prompt, [CREATE_BRIEFING_TOOL], messages,
            candidate_index, full_name,
            tool_choice={"type": "tool", "name": "create_briefing"},
            phase="phase2_retry", turn=2,
        )
        if retry_response is None:
            return None

        retry_block = next(
            (b for b in retry_response.content
             if b.type == "tool_use" and b.name == "create_briefing"),
            None,
        )
        if retry_block is None:
            _log_error(
                f"candidate_{candidate_index} ({full_name}): "
                f"Phase 2 correction retry missing tool call"
            )
            return None

        try:
            briefing = CandidateBriefing.model_validate(retry_block.input)
            validated = briefing.model_dump()
        except ValidationError as e2:
            _log_error(
                f"candidate_{candidate_index} ({full_name}): "
                f"Phase 2 validation still failing after correction: {e2.errors()}"
            )
            return None

    # Write to output file via the atomic Python writer (not a Claude-visible tool)
    from tools.write_output import write_output
    write_output(validated)

    return validated


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_candidate_agent(candidate: dict[str, Any], candidate_index: int) -> dict[str, Any]:
    """
    Run the two-phase research + synthesis pipeline for a single candidate.

    Args:
        candidate: dict with keys full_name, current_employer, current_title, linkedin_url
        candidate_index: 1-based index for candidate_id

    Returns:
        Always a dict — either a valid CandidateBriefing or a minimal error object.
    """
    full_name = candidate.get("full_name", "Unknown")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        error_msg = "ANTHROPIC_API_KEY not set"
        _log_error(f"candidate_{candidate_index}: {error_msg}")
        return _minimal_error_object(candidate_index, full_name, error_msg)

    client = anthropic.Anthropic(api_key=api_key)

    try:
        research_system_prompt = _load_research_system_prompt()
    except FileNotFoundError as e:
        _log_error(str(e))
        return _minimal_error_object(candidate_index, full_name, str(e))

    # ── Phase 0: Pre-flight identity check ────────────────────────────────────
    # Cheap gate (≤2 searches + optional fetch) that catches wrong-employer or
    # wrong-person intake data before the full 5-slot research budget is spent.
    preflight = _run_phase0_preflight(client, candidate, candidate_index)
    # Enrich the candidate dict so Phase 1 can reference the verdict inline.
    candidate = dict(candidate)
    candidate["_phase0_verdict"] = preflight["verdict"]
    candidate["_phase0_evidence"] = preflight["evidence"]
    if preflight["correct_employer"]:
        candidate["_phase0_correct_employer"] = preflight["correct_employer"]
    if preflight["correct_title"]:
        candidate["_phase0_correct_title"] = preflight["correct_title"]

    # ── Phase 1: Research ──────────────────────────────────────────────────────
    raw_research_text = _run_phase1_research(
        client, candidate, candidate_index, research_system_prompt
    )
    if not raw_research_text:
        error_msg = "Phase 1 research produced no output (API error or empty response)"
        _log_error(f"candidate_{candidate_index} ({full_name}): {error_msg}")
        return _minimal_error_object(candidate_index, full_name, error_msg)

    # ── research_text preparation: compress if needed, never hard-truncate tail ─
    research_text = _prepare_research_for_phase2(
        client, raw_research_text, candidate_index, full_name
    )

    # ── Phase 2: Synthesis ─────────────────────────────────────────────────────
    result = _run_phase2_synthesis(
        client, candidate, candidate_index, research_text, SYNTHESIS_SYSTEM_PROMPT
    )
    if result is None:
        error_msg = "Phase 2 synthesis failed to produce a valid briefing"
        _log_error(f"candidate_{candidate_index} ({full_name}): {error_msg}")
        return _minimal_error_object(candidate_index, full_name, error_msg)

    return result


def _minimal_error_object(index: int, full_name: str, reason: str) -> dict[str, Any]:
    """Return a minimal object with error info when the agent fails."""
    return {
        "candidate_id": f"candidate_{index}",
        "full_name": full_name,
        "current_role": {"title": "[ERROR]", "employer": "[ERROR]", "tenure_years": 0.0},
        "career_narrative": (
            f"[ERROR] Agent failed to produce output for this candidate. "
            f"Reason: {reason}. "
            f"This entry should be re-run. "
            f"No data is available."
        ),
        "experience_tags": ["[ERROR]", "re-run required"],
        "firm_aum_context": "[ERROR]",
        "mobility_signal": {"score": 1, "rationale": "[ERROR]"},
        "role_fit": {
            "role": "Head of Distribution / National BDM",
            "score": 1,
            "justification": "[ERROR] Could not assess. Re-run required.",
        },
        "outreach_hook": "[ERROR] Re-run required",
    }