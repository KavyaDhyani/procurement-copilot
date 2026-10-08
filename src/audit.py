"""Append-only log of human review decisions.

The copilot only recommends. Whatever a person decides is recorded here with the
recommendation they were shown, so there is a trail from evidence to decision.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.config import ROOT
from src.models import AnalysisResult

AUDIT_LOG = ROOT / "var" / "human_review_log.jsonl"

HUMAN_DECISIONS = (
    "Send to the listed approvers",
    "Return to requester for information",
    "Escalate for manual review",
    "Use existing tool - close request",
    "Reject request",
)


def record_human_decision(result: AnalysisResult, reviewer: str, decision: str, note: str, path: Path | None = None) -> dict:
    path = path or AUDIT_LOG
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_id": result.decision.request_id,
        "reviewer": reviewer.strip(),
        "human_decision": decision,
        "note": note.strip(),
        "copilot": {"architecture": result.architecture, "model": result.model, "action": result.action,
                    "required_approvals": result.decision.required_approvals, "risk_flags": result.decision.risk_flags,
                    "degraded": result.degraded},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_human_decisions(request_id: str | None = None, path: Path | None = None) -> list[dict]:
    path = path or AUDIT_LOG
    if not path.is_file():
        return []
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in entries if request_id is None or e["request_id"] == request_id]
