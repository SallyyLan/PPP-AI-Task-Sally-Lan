"""
tests/test_tools.py — Unit tests for write_output tool.

Run with: pytest tests/ -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def _valid_candidate_dict() -> dict:
    """Return a valid CandidateBriefing dict based on the Appendix C example."""
    return {
        "candidate_id": "candidate_1",
        "full_name": "Andrew Swan",
        "current_role": {
            "title": "Head of Distribution",
            "employer": "Apex Asset Management",
            "tenure_years": 3.5,
        },
        "career_narrative": (
            "Andrew Swan has spent over fifteen years in Australian funds management distribution, "
            "building a career that spans both wholesale and institutional channels. "
            "He joined Apex Asset Management in 2021 as Head of Distribution, overseeing a national "
            "team of five BDMs with responsibility for platform, IFA, and superannuation fund relationships. "
            "Prior to Apex, he held senior roles at two mid-tier active managers, progressing from "
            "regional BDM to national sales leadership."
        ),
        "experience_tags": [
            "Australian wholesale distribution",
            "Institutional sales",
            "Platform relationships",
            "National sales team leadership",
            "Super fund engagement",
            "Active equity and fixed income",
        ],
        "firm_aum_context": (
            "Apex Asset Management is a mid-tier Australian active manager with approximately "
            "$12B AUM [ESTIMATED — based on public fund registry data and press coverage]; "
            "institutional and wholesale focus with a strong domestic equities franchise."
        ),
        "mobility_signal": {
            "score": 3,
            "rationale": (
                "Swan has been at Apex for approximately 3.5 years with no visible recent promotion, "
                "and Apex has undergone moderate ownership restructuring in 2023 that may have "
                "affected internal progression pathways."
            ),
        },
        "role_fit": {
            "role": "Head of Distribution / National BDM",
            "score": 8,
            "justification": (
                "Swan currently holds a directly comparable role — Head of Distribution at a "
                "mid-tier active manager — and meets five of the six criteria strongly. "
                "His team management experience (five direct reports) and platform/IFA network "
                "are well-aligned to the target role. "
                "The minor gap is limited public evidence of deep super fund relationships at "
                "the senior consultant level, though his employer profile suggests exposure."
            ),
        },
        "outreach_hook": (
            "Given Apex's recent ownership transition, I thought it worth reaching out — "
            "we are working with a well-capitalised active manager looking to build out their "
            "distribution leadership, and your background across wholesale and platform channels "
            "is exactly the profile they have in mind"
        ),
    }


# ── write_output tests ─────────────────────────────────────────────────────────

def test_write_output_creates_file():
    """write_output should create output.json with a candidates array of length 1."""
    from tools import write_output as wo_module

    candidate = _valid_candidate_dict()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Patch OUTPUT_PATH to use temp directory
        original_path = wo_module.OUTPUT_PATH
        tmp_output = os.path.join(tmpdir, "output", "output.json")
        wo_module.OUTPUT_PATH = tmp_output

        try:
            result = wo_module.write_output(candidate)
            assert result.get("success") is True
            assert os.path.exists(tmp_output), "output.json was not created"

            with open(tmp_output, "r", encoding="utf-8") as f:
                data = json.load(f)

            assert "candidates" in data
            assert len(data["candidates"]) == 1
        finally:
            wo_module.OUTPUT_PATH = original_path


def test_write_output_appends():
    """Writing two candidates sequentially should produce an array of length 2."""
    from tools import write_output as wo_module

    candidate1 = _valid_candidate_dict()
    candidate2 = dict(_valid_candidate_dict())
    candidate2["candidate_id"] = "candidate_2"
    candidate2["full_name"] = "Cathy Hales"

    with tempfile.TemporaryDirectory() as tmpdir:
        original_path = wo_module.OUTPUT_PATH
        tmp_output = os.path.join(tmpdir, "output", "output.json")
        wo_module.OUTPUT_PATH = tmp_output

        try:
            wo_module.write_output(candidate1)
            result2 = wo_module.write_output(candidate2)

            assert result2.get("success") is True
            assert result2.get("total_candidates") == 2

            with open(tmp_output, "r", encoding="utf-8") as f:
                data = json.load(f)

            assert len(data["candidates"]) == 2
        finally:
            wo_module.OUTPUT_PATH = original_path
