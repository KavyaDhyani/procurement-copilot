"""Drives the Streamlit app headlessly. The suite disables the model (conftest), so runs take the deterministic path."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from src import audit
from src.config import ROOT

@pytest.fixture
def app(api_url, monkeypatch, tmp_path):
    monkeypatch.setenv("VENDOR_RISK_BASE_URL", api_url)
    monkeypatch.delenv("MOCK_VENDOR_RISK_FILE", raising=False)
    monkeypatch.setattr(audit, "AUDIT_LOG", tmp_path / "human_review_log.jsonl")
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()


def text(app: AppTest) -> str:
    return " ".join(str(el.value) for kind in ("markdown", "subheader", "caption", "info", "success", "warning", "error")
                    for el in getattr(app, kind))


def test_page_loads_with_request_details_before_any_analysis(app):
    assert not app.exception
    assert "Request details" in text(app) and "SignFlow Add-on" in text(app)
    assert "Run analysis" in [b.label for b in app.button]


def test_run_shows_recommendation_evidence_and_human_decision(app):
    app.selectbox[0].select("REQ-1005")
    next(b for b in app.button if b.label == "Run analysis").click().run()
    assert not app.exception
    page = text(app)
    assert "Route for required reviews before approval" in page
    assert "`Security`" in page and "`budget_insufficient`" in page
    assert "Human decision" in page and "deterministic-only" in page       # degraded mode is stated, not hidden
    assert len(app.dataframe) >= 3                                          # evidence tables + ledger


def test_human_decision_is_recorded_with_the_reviewers_name(app, tmp_path):
    next(b for b in app.button if b.label == "Run analysis").click().run()
    app.text_input[0].set_value("Priya Shah")
    next(b for b in app.button if b.label == "Record decision").click().run()
    assert not app.exception
    logged = audit.load_human_decisions("REQ-1001", tmp_path / "human_review_log.jsonl")
    assert len(logged) == 1 and logged[0]["reviewer"] == "Priya Shah"
    assert logged[0]["copilot"]["action"] == "proceed_to_standard_approval"


def test_compare_mode_runs_both_architectures(app):
    app.radio[1].set_value("compare")
    next(b for b in app.button if b.label == "Run analysis").click().run()
    assert not app.exception
    assert "Architecture comparison for this request" in text(app)
