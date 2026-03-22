"""
run.py — CLI orchestrator for PPP candidate research agent.

Usage:
    python run.py [path/to/candidates.csv]

Defaults to data/candidates.csv if no path given.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Required columns — linkedin_url is optional; rows without it are still processed.
REQUIRED_COLUMNS = {"full_name", "current_employer", "current_title"}

# Original 5 candidate names from Appendix A (for supplementary labelling)
ORIGINAL_CANDIDATES = {
    "Andrew Swan",
    "Cathy Hales",
    "Jason Ennis",
    "Deborah Southon",
    "Nikki Thomas",
}

TMP_DIR = Path(__file__).parent / ".tmp"
ERRORS_LOG = TMP_DIR / "errors.log"
TOKENS_LOG = TMP_DIR / "tokens.log"

# Pause between candidates to let the Anthropic TPM window recover.
BETWEEN_CANDIDATES_DELAY = 10  # seconds


def clear_tmp() -> None:
    """Clear .tmp/ from previous run and recreate it."""
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    ERRORS_LOG.touch()
    TOKENS_LOG.touch()


def load_candidates(csv_path: str) -> list[dict]:
    """Load and validate candidates CSV."""
    path = Path(csv_path)
    if not path.exists():
        print(f"[ERROR] CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    candidates = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            print("[ERROR] CSV file is empty or malformed.", file=sys.stderr)
            sys.exit(1)

        actual_columns = set(reader.fieldnames)
        missing = REQUIRED_COLUMNS - actual_columns
        if missing:
            print(
                f"[ERROR] CSV is missing required columns: {', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            sys.exit(1)

        for row in reader:
            candidates.append(dict(row))

    return candidates


def print_summary_table(candidates: list[dict]) -> None:
    """Print a table of candidates with original/supplementary labels."""
    print(f"\n{'#':<4} {'Name':<25} {'Employer':<30} {'Title':<35} {'Source'}")
    print("-" * 100)
    for i, c in enumerate(candidates, 1):
        name = c.get("full_name", "")
        source = "Original" if name in ORIGINAL_CANDIDATES else "Supplementary"
        print(
            f"{i:<4} {name:<25} {c.get('current_employer',''):<30} "
            f"{c.get('current_title',''):<35} {source}"
        )
    print()


def reset_output() -> None:
    """Reset output.json to empty state before a run."""
    output_path = Path(__file__).parent / "output" / "output.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"candidates": []}, f)


def main() -> None:
    parser = argparse.ArgumentParser(description="PPP Candidate Research Agent")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default="data/candidates.csv",
        help="Path to candidates CSV (default: data/candidates.csv)",
    )
    args = parser.parse_args()

    # Setup
    clear_tmp()
    reset_output()

    candidates = load_candidates(args.csv_path)
    total = len(candidates)

    print(f"\n{'='*60}")
    print(f"  PPP Candidate Research Agent")
    print(f"{'='*60}")
    print(f"  Loaded {total} candidate(s) from {args.csv_path}")

    original_count = sum(1 for c in candidates if c.get("full_name") in ORIGINAL_CANDIDATES)
    supplementary_count = total - original_count
    if supplementary_count > 0:
        print(f"  Original (Appendix A): {original_count}")
        print(f"  Supplementary (added by submitter): {supplementary_count}")

    print_summary_table(candidates)

    # Import agent after env is loaded
    from agent import run_candidate_agent
    from tools.write_output import write_output

    succeeded = 0
    failed = 0
    failed_names = []

    for i, candidate in enumerate(candidates, 1):
        name = candidate.get("full_name", f"Candidate {i}")
        is_supplementary = name not in ORIGINAL_CANDIDATES

        print(f"[{i}/{total}] Starting: {name}...", end="", flush=True)

        # Label supplementary candidates
        if is_supplementary:
            candidate = dict(candidate)
            candidate["_supplementary"] = True

        try:
            result = run_candidate_agent(candidate, i)

            if result.get("career_narrative", "").startswith("[ERROR]"):
                print(f" FAILED")
                failed += 1
                failed_names.append((name, result.get("career_narrative", "")))
                # Agent did not write this error object — write it now
                write_output(result)
            else:
                # Agent already wrote via write_output tool — just track success
                print(f" Done")
                succeeded += 1

        except Exception as e:
            print(f" EXCEPTION: {e}")
            failed += 1
            failed_names.append((name, str(e)))

            # Write minimal error object so output.json always has one entry per row
            try:
                from agent import _minimal_error_object
                err_obj = _minimal_error_object(i, name, str(e))
                write_output(err_obj)
            except Exception:
                pass

        if i < total:
            time.sleep(BETWEEN_CANDIDATES_DELAY)

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Run complete: {succeeded} succeeded, {failed} failed")
    if failed_names:
        print(f"\n  Failed candidates:")
        for fname, reason in failed_names:
            print(f"    - {fname}: {reason[:80]}")
    print(f"\n  Output: {Path('output/output.json').resolve()}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
