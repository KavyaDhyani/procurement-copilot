"""Tools, the vendor-risk client and the injection scanner against the real data snapshot and mock API."""
from __future__ import annotations

from datetime import date
from src import data_access
from src.config import Settings
from src.injection import scan_text
from src.models import ProcurementRequest
from src.tools import TOOLS, RunContext, run_policy_engine
from src.vendor_client import fetch_vendor_risk

REF = date(2026, 9, 30)


def ctx_for(request_id: str, settings: Settings) -> RunContext:
    raw = data_access.get_request(request_id, settings.data_dir)
    return RunContext(ProcurementRequest.from_raw(raw), settings, data_access.load_reference_date(settings.data_dir))


def test_at_least_three_tools_and_one_deterministic():
    assert len(TOOLS) >= 3
    assert sum(spec.deterministic for spec in TOOLS.values()) >= 1


# --- vendor-risk client ----------------------------------------------------------------------

def test_client_distinguishes_ok_missing_and_outage(settings):
    assert fetch_vendor_risk("BrandBoard", settings).outcome == "ok"
    missing = fetch_vendor_risk("Nobody Inc", settings)
    assert (missing.outcome, missing.http_status) == ("not_found", 404)
    outage = fetch_vendor_risk("NimbusAI", settings)
    assert (outage.outcome, outage.http_status, outage.attempts) == ("unavailable", 503, 2)


def test_client_reports_unreachable_service_instead_of_raising():
    dead = Settings(vendor_risk_base_url="http://127.0.0.1:9", vendor_risk_timeout_seconds=0.5)
    result = fetch_vendor_risk("PixelCraft", dead, retries=0)
    assert result.outcome == "unavailable" and result.record is None


def test_mock_api_handles_percent_and_slash_in_vendor_names(scratch_data):
    """Regression: the starter URL-decoded the path a second time, so 'Acme 100%25 Co' was looked up as 'Acme 100% Co'.

    Goes through a real HTTP server on purpose: Starlette's TestClient decodes paths differently from uvicorn.
    """
    for name in ("Acme 100%25 Co", "A/B Labs", "Fifty% Off"):
        scratch_data.set_risk(name, {"risk_level": "low", "security_review_status": "approved",
                                     "last_review_date": "2026-01-01", "notes": ""})
        result = fetch_vendor_risk(name, scratch_data.settings)
        assert result.outcome == "ok" and result.record["vendor_name"] == name


def test_slow_service_times_out_as_unavailable(scratch_data):
    scratch_data.set_risk("PixelCraft", {"simulate_delay_seconds": 1.5, "security_review_status": "approved"})
    slow = Settings(data_dir=scratch_data.dir, vendor_risk_base_url=scratch_data.settings.vendor_risk_base_url,
                    vendor_risk_timeout_seconds=0.3)
    assert fetch_vendor_risk("PixelCraft", slow, retries=0).outcome == "unavailable"


# --- tools -----------------------------------------------------------------------------------

def test_budget_tool_reports_shortfall(settings):
    entry = ctx_for("REQ-1005", settings).call("check_budget", {"requester_id": "E003", "annual_cost_usd": 22000})
    assert entry.output["within_budget"] is False and entry.output["shortfall_usd"] == 4000
    assert entry.output["department"] == "Sales" and "department_budgets.csv" in entry.reference


def test_budget_tool_handles_unknown_requester_and_unbudgeted_department(settings):
    ctx = ctx_for("REQ-1001", settings)
    assert ctx.call("check_budget", {"requester_id": "E999", "annual_cost_usd": 100}).status == "unknown_requester"
    # E007 is in "Go To Market", which has no row in department_budgets.csv
    assert ctx.call("check_budget", {"requester_id": "E007", "annual_cost_usd": 100}).status == "no_budget_record"


def test_catalog_search_always_includes_structural_matches_and_adds_keyword_hits(settings):
    ctx = ctx_for("REQ-1002", settings)        # BrandBoard, Design & Creative
    plain = ctx.call("search_software_catalog", {"keywords": []}).output["matches"]
    assert {m["product_name"] for m in plain} == {"PixelCraft Pro", "CreativeSuite"}
    with_keywords = ctx.call("search_software_catalog", {"keywords": ["dashboards"]}).output["matches"]
    assert "MetricLoop" in {m["product_name"] for m in with_keywords}
    assert all(m["last_purchase"] for m in plain)      # purchase history is joined in


def test_registry_tool_computes_expiry_from_the_reference_date(settings):
    out = ctx_for("REQ-1007", settings).call("lookup_vendor_registry", {"vendor_name": "signalwatch"}).output
    assert (out["review_age_days"], out["review_current"]) == (456, False)


def test_identical_tool_requests_run_once(settings):
    ctx = ctx_for("REQ-1001", settings)
    first = ctx.call("get_vendor_risk", {"vendor_name": "SignFlow"})
    again = ctx.call("get_vendor_risk", {"vendor_name": " SignFlow "})
    assert first is again and len(ctx.ledger) == 1


def test_bad_tool_requests_are_recorded_not_raised(settings):
    ctx = ctx_for("REQ-1001", settings)
    assert ctx.call("delete_budget", {}).status == "unknown_tool"
    assert ctx.call("lookup_vendor_registry", {}).status == "invalid_arguments"


def test_policy_engine_runs_mandatory_checks_itself_when_the_agent_ran_none(settings):
    ctx = ctx_for("REQ-1009", settings)
    assessment = run_policy_engine(ctx)
    assert [e.tool for e in ctx.ledger] == ["check_budget", "search_software_catalog", "lookup_vendor_registry", "get_vendor_risk"]
    assert all(e.requested_by == "harness" for e in ctx.ledger)
    assert "vendor_risk_unavailable" in assessment.risk_flags


def test_policy_engine_ignores_model_supplied_arguments_for_mandatory_checks(settings):
    """An agent that looks up the wrong vendor or a smaller cost cannot steer the rules."""
    ctx = ctx_for("REQ-1005", settings)       # GrowthForge, $22,000, Sales has $18,000
    ctx.call("check_budget", {"requester_id": "E004", "annual_cost_usd": 10})
    ctx.call("lookup_vendor_registry", {"vendor_name": "PixelCraft"})
    ctx.call("get_vendor_risk", {"vendor_name": "PixelCraft"})
    assessment = run_policy_engine(ctx)
    assert "budget_insufficient" in assessment.risk_flags
    assert {"Security", "Privacy", "Legal", "Finance"} <= set(assessment.required_approvals)


def test_edited_snapshot_is_picked_up_without_restart(scratch_data):
    """Hidden cases swap data values behind the same interface; nothing may be cached."""
    ctx = ctx_for("REQ-1001", scratch_data.settings)
    assert ctx.call("get_vendor_risk", {"vendor_name": "SignFlow"}).output["security_review_status"] == "approved"
    scratch_data.set_risk("SignFlow", {"security_review_status": "expired", "last_review_date": "2025-01-01", "risk_level": "high"})
    fresh = ctx_for("REQ-1001", scratch_data.settings)
    assert "vendor_review_expired" in run_policy_engine(fresh).risk_flags


# --- injection scanner -----------------------------------------------------------------------

def test_scanner_catches_the_known_injection():
    text = data_access.get_request("REQ-1006")["business_justification"]
    assert len(scan_text(text)) >= 2


def test_scanner_has_no_false_positives_on_legitimate_dataset_text():
    texts = [r["business_justification"] for r in data_access.load_requests() if r["request_id"] != "REQ-1006"]
    texts += [v["notes"] for v in data_access.load_vendors()] + [c["notes"] for c in data_access.load_software_catalog()]
    texts += ["Current assessment.", "Reassessment required before expanded production access.",
              "Approved for source-code use when repository controls are enabled.",
              "Sensitive-data use requires Security and Privacy review.",
              "Automation lets agents skip the manual triage process and flag non-standard payment terms."]
    assert [t for t in texts if scan_text(t)] == []


# --- model client: provider differences ------------------------------------------------------

def test_rate_limit_delay_is_read_from_header_or_body_or_backed_off():
    import httpx

    from src.llm import _error_message, _retry_delay

    groq = httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}})
    gemini = httpx.Response(429, json=[{"error": {"message": "quota", "details": [{"retryDelay": "23s"}]}}])
    bare = httpx.Response(429, text="too many requests")
    assert _retry_delay(groq, 0) == 7
    assert _retry_delay(gemini, 0) == 24
    assert [_retry_delay(bare, attempt) for attempt in range(3)] == [5, 15, 45]
    assert _error_message(gemini) == "quota" and _error_message(groq) == "slow down"


def test_provider_is_selected_from_whichever_key_is_set(monkeypatch):
    from src.llm import LLMSettings

    for name in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "MODEL_NAME", "GROQ_API_KEY", "GROQ_KEY", "GROQ_MODEL",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY", "GEMINI_MODEL", "LLM_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_KEY", "g-key")
    gemini = LLMSettings.from_env()
    assert (gemini.provider, gemini.api_key, gemini.reasoning_effort) == ("gemini", "g-key", None)
    assert "generativelanguage.googleapis.com" in gemini.base_url

    monkeypatch.setenv("GROQ_KEY", "q-key")                 # both set: Groq unless told otherwise
    assert LLMSettings.from_env().provider == "groq"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "some-model")
    chosen = LLMSettings.from_env()
    assert (chosen.provider, chosen.api_key, chosen.model) == ("gemini", "g-key", "some-model")


# --- launcher plumbing -----------------------------------------------------------------------

def test_scripts_switch_to_the_project_virtualenv_only_when_needed(monkeypatch, tmp_path):
    import project_env

    venv_python = project_env.ROOT / ".venv" / ("Scripts/python.exe" if project_env.os.name == "nt" else "bin/python")
    if not venv_python.is_file():
        assert project_env.project_python() is None                      # nothing to switch to
        return
    monkeypatch.delenv("COPILOT_NO_VENV_SWITCH", raising=False)
    assert project_env.project_python(prefix=str(tmp_path)) == venv_python       # started from some other Python
    assert project_env.project_python(prefix=str(project_env.ROOT / ".venv")) is None   # already inside .venv
    monkeypatch.setenv("COPILOT_NO_VENV_SWITCH", "1")
    assert project_env.project_python(prefix=str(tmp_path)) is None              # explicit opt-out


def test_mock_api_root_points_to_the_ui():
    from fastapi.testclient import TestClient

    from mock_api.app import app

    body = TestClient(app).get("/").json()
    assert body["copilot_ui"] == "http://127.0.0.1:8501" and "/health" in body["endpoints"]
