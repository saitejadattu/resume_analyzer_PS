"""Skill knowledge base: loads ``data/skills.yaml`` and compiles matchers.

Centralises all skill-normalisation logic so both the keyword matcher and the
tech detector share one source of truth (no duplicated code — Step 20).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from . import config
from .utils import get_logger

logger = get_logger("skills_kb")


class SkillsKB:
    """Compiled, reusable skill knowledge base."""

    def __init__(
        self,
        canonical_skills: dict[str, list[str]],
        phrase_rules: dict[str, list[str]],
        genai_markers: list[str],
    ) -> None:
        self.canonical_skills = canonical_skills
        self.phrase_rules = phrase_rules
        self.genai_markers = {m.lower() for m in genai_markers}

        # alias (lower) -> canonical name. Includes the canonical name itself.
        self._alias_to_canonical: dict[str, str] = {}
        for canonical, aliases in canonical_skills.items():
            self._alias_to_canonical[canonical.lower()] = canonical
            for alias in aliases:
                self._alias_to_canonical[alias.lower()] = canonical

        # Pre-compile one word-boundary regex per canonical skill, matching any
        # of its surface forms. Longest surface forms first to avoid partials.
        self._skill_patterns: dict[str, re.Pattern[str]] = {}
        for canonical, aliases in canonical_skills.items():
            forms = sorted(
                {canonical, *aliases}, key=len, reverse=True
            )
            escaped = [self._boundary_pattern(f) for f in forms]
            self._skill_patterns[canonical] = re.compile(
                "|".join(escaped), re.IGNORECASE
            )

        # Pre-compile phrase-rule patterns.
        self._phrase_patterns: list[tuple[re.Pattern[str], list[str]]] = []
        for phrase, techs in phrase_rules.items():
            self._phrase_patterns.append(
                (re.compile(re.escape(phrase), re.IGNORECASE), techs)
            )

    @staticmethod
    def _boundary_pattern(form: str) -> str:
        r"""Build a regex that matches ``form`` as a whole token.

        Uses lookarounds instead of ``\b`` because skills like ``C++``,
        ``.NET`` and ``node.js`` contain non-word characters that break ``\b``.
        """
        escaped = re.escape(form)
        # Not preceded/followed by an alphanumeric (so "java" != "javascript",
        # but "C++" and ".net" still match).
        return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def canonical_of(self, token: str) -> str | None:
        """Return the canonical skill name for a raw token, if known."""
        return self._alias_to_canonical.get(token.strip().lower())

    def skill_in_text(self, canonical: str, text: str) -> bool:
        """True if a canonical skill (any surface form) appears in ``text``."""
        pattern = self._skill_patterns.get(canonical)
        if pattern is None:
            # Unknown skill (e.g. from a JD not in the KB): match literally.
            pattern = re.compile(self._boundary_pattern(canonical), re.IGNORECASE)
        return bool(pattern.search(text))

    def detect_skills(self, text: str) -> list[str]:
        """Return all canonical skills whose surface forms appear in ``text``."""
        if not text:
            return []
        found: list[str] = []
        for canonical, pattern in self._skill_patterns.items():
            if pattern.search(text):
                found.append(canonical)
        return found

    def infer_from_phrases(self, text: str) -> list[str]:
        """Infer technologies from descriptive phrases (Step 8, rule-based)."""
        if not text:
            return []
        inferred: list[str] = []
        for pattern, techs in self._phrase_patterns:
            if pattern.search(text):
                inferred.extend(techs)
        return inferred

    def is_genai(self, techs: list[str]) -> bool:
        """True if any technology marks the set as GenAI (Step 12 bonus)."""
        return any(t.lower() in self.genai_markers for t in techs)


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def load_kb(skills_file: str | None = None) -> SkillsKB:
    """Load and cache the skills knowledge base (parsed once per process)."""
    path = Path(skills_file) if skills_file else config.SKILLS_FILE
    if not path.exists():
        raise FileNotFoundError(f"Skills knowledge base not found: {path}")

    data = _load_yaml(path)
    kb = SkillsKB(
        canonical_skills=data.get("canonical_skills", {}),
        phrase_rules=data.get("phrase_rules", {}),
        genai_markers=data.get("genai_markers", []),
    )
    logger.info(
        "Loaded skills KB: %d skills, %d phrase rules",
        len(kb.canonical_skills),
        len(kb.phrase_rules),
    )
    return kb
