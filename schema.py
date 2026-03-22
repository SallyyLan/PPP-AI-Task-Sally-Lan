"""
schema.py — Single source of truth for all output types.
Every other module imports from here. Matches Section 4 of the PPP brief exactly.
"""

from __future__ import annotations

import re
from typing import ClassVar, List
from pydantic import BaseModel, field_validator


class CurrentRole(BaseModel):
    title: str
    employer: str
    tenure_years: int

    @field_validator("tenure_years")
    @classmethod
    def tenure_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("tenure_years must be >= 0")
        return v


class MobilitySignal(BaseModel):
    score: int
    rationale: str

    @field_validator("score")
    @classmethod
    def score_range(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError(f"mobility score must be 1–5, got {v}")
        return v


class RoleFit(BaseModel):
    role: str
    score: int
    justification: str

    EXACT_ROLE: ClassVar[str] = "Head of Distribution / National BDM"

    @field_validator("role")
    @classmethod
    def role_exact(cls, v: str) -> str:
        expected = "Head of Distribution / National BDM"
        if v != expected:
            raise ValueError(
                f'role must be exactly "{expected}", got "{v}"'
            )
        return v

    @field_validator("score")
    @classmethod
    def score_range(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError(f"role_fit score must be 1–10, got {v}")
        return v


class CandidateBriefing(BaseModel):
    candidate_id: str
    full_name: str
    current_role: CurrentRole
    career_narrative: str
    experience_tags: List[str]
    firm_aum_context: str
    mobility_signal: MobilitySignal
    role_fit: RoleFit
    outreach_hook: str

    @field_validator("career_narrative")
    @classmethod
    def narrative_sentence_count(cls, v: str) -> str:
        # Strip bracketed confidence labels before counting sentences.
        # Multi-sentence labels like [NOT FOUND — searched X. Zero results.]
        # must not inflate the count — only the plain prose sentences outside
        # labels are counted toward the 3–4 requirement.
        text_for_counting = re.sub(r"\[[^\]]*\]", "", v)
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text_for_counting.strip()) if p.strip()]
        count = len(parts)
        if not 3 <= count <= 4:
            raise ValueError(
                f"career_narrative must be 3–4 sentences (detected {count} sentences)"
            )
        return v

    @field_validator("experience_tags")
    @classmethod
    def tags_minimum(cls, v: List[str]) -> List[str]:
        if len(v) < 2:
            raise ValueError(
                f"experience_tags must have at least 2 items, got {len(v)}"
            )
        return v

    @field_validator("outreach_hook")
    @classmethod
    def hook_one_sentence(cls, v: str) -> str:
        # Strip bracketed confidence labels before counting sentences, then
        # split on punctuation followed by whitespace + uppercase letter.
        text_for_counting = re.sub(r"\[[^\]]*\]", "", v)
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text_for_counting.strip()) if p.strip()]
        if len(parts) > 1:
            raise ValueError(
                "outreach_hook must be a single sentence (no full stops mid-sentence)"
            )
        return v


class OutputFile(BaseModel):
    candidates: List[CandidateBriefing]
