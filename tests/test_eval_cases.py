"""The evaluation set itself: well-formed, and satisfiable by the deterministic layer where no judgement is needed."""
from __future__ import annotations

import json

import pytest

from evals.run_comparison import FIXTURE_DIR, score
from src.config import ROOT, Settings
from src.models import ACTION_LABELS
from src.solution import analyze_request

CASES = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
# Cases whose expected outcome depends on the model's reading of the request (overlap judgement, free-text data class).
NEEDS_MODEL_JUDGEMENT = {"DS-08", "FX-02", "FX-03"}


def test_cases_are_well_formed_and_cover_every_edge_case_in_the_brief():
    assert len({c["case_id"] for c in CASES}) == len(CASES)
    for case in CASES:
        assert set(case["expected"]["actions"]) <= set(ACTION_LABELS)
    covered = " ".join(c["edge_case"] for c in CASES)
    for edge in ("incomplete", "existing tool", "conflicting", "expired", "security-sensitive", "threshold",
                 "prompt injection", "API unavailable"):
        assert edge in covered, f"no case covers: {edge}"


@pytest.mark.parametrize("case", [c for c in CASES if c["case_id"] not in NEEDS_MODEL_JUDGEMENT], ids=lambda c: c["case_id"])
def test_deterministic_layer_alone_satisfies_cases_that_need_no_judgement(case, api_url, monkeypatch, scripted_llm):
    """With the model switched off, code alone must already get the rules right on these cases.

    This is the floor the product stands on when the model is wrong or unavailable.
    """
    if case["dataset"] == "fixtures":
        data_dir = FIXTURE_DIR
        monkeypatch.setenv("MOCK_VENDOR_RISK_FILE", str(FIXTURE_DIR / "vendor_risk.json"))
    else:
        data_dir = ROOT / "data"
        monkeypatch.delenv("MOCK_VENDOR_RISK_FILE", raising=False)
    settings = Settings(data_dir=data_dir, vendor_risk_base_url=api_url, vendor_risk_timeout_seconds=1.0)
    raw = next(r for r in json.loads((data_dir / "requests.json").read_text(encoding="utf-8")) if r["request_id"] == case["request_id"])
    result = analyze_request(raw, "single", settings, scripted_llm({}))       # every model call fails -> deterministic only
    outcome = score(result, case["expected"])
    assert result.degraded
    assert outcome["correct_next_action"] and outcome["policy_followed"] and outcome["human_escalation_correct"], outcome["notes"]
