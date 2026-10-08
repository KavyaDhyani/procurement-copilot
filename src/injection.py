"""Heuristic scan for instruction-like text inside business data (policy section 9).

This is a tripwire for reviewers, not the defence. The defence is structural:
approvals, risk flags and the human-review requirement are computed in code, so
text that fools the model still cannot change them. Patterns therefore favour
recall; a false positive only costs a reviewer a glance.
"""
from __future__ import annotations

import re

_PATTERNS = [
    r"\b(ignore|disregard|forget|override|bypass|circumvent)\b[^.\n]{0,60}\b(rules?|instructions?|polic(y|ies)|controls?|guardrails?|safeguards?|approvals?|reviews?)\b",
    r"\b(treat|consider|mark|record|flag|classify)\b[^.\n]{0,40}\bas\b[^.\n]{0,30}\b(approved|pre-?approved|compliant|low[- ]risk|cleared|exempt)\b",
    r"\b(pre-?approved|already approved|cfo[- ]approved|approved by (the )?(cfo|ceo|security|legal|finance))\b",
    r"\bapprove\b[^.\n]{0,30}\b(immediately|now|automatically|without)\b",
    r"\bauto[- ]?approv",
    r"\b(system prompt|system message|developer message|you are now|new instructions?|as an ai|assistant:|system:)",
    r"\b(do not|don't|never)\b[^.\n]{0,30}\b(flag|escalate|mention|report|require|route)\b",
    r"\bno\b[^.\n]{0,20}\b(security|privacy|legal|finance|human)\b[^.\n]{0,15}\b(review|approval|check)\b[^.\n]{0,15}\b(needed|required|necessary)\b",
    r"\b(reveal|print|output|expose)\b[^.\n]{0,30}\b(secret|api key|credentials?|system prompt|instructions)\b",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PATTERNS]


def scan_text(text: str | None) -> list[str]:
    """Return the instruction-like snippets found in `text` (deduplicated, in order)."""
    if not text:
        return []
    found: list[str] = []
    for pattern in _COMPILED:
        for match in pattern.finditer(text):
            snippet = " ".join(match.group(0).split())[:120]
            if not any(snippet in f or f in snippet for f in found):
                found.append(snippet)
    return found


def scan_fields(fields: dict[str, str | None]) -> list[dict[str, str]]:
    """Scan labelled business-data fields; returns [{"field": ..., "snippet": ...}]."""
    return [{"field": label, "snippet": s} for label, text in fields.items() for s in scan_text(text)]
