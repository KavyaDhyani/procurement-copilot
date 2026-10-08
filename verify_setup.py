from __future__ import annotations

import csv
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    from project_env import use_project_virtualenv

    use_project_virtualenv(__file__)

REQUIRED_MODULES = [
    "fastapi",
    "uvicorn",
    "pydantic",
    "pandas",
    "requests",
    "dotenv",
    "streamlit",
    "httpx",
]


def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"[ OK ] {message}")


def check_python() -> None:
    if sys.version_info < (3, 11):
        fail(f"Python 3.11+ is required/recommended. Detected: {sys.version.split()[0]}")
    ok(f"Python {sys.version.split()[0]}")


def check_imports() -> None:
    missing: list[str] = []
    for module_name in REQUIRED_MODULES:
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
    if missing:
        fail(
            "Missing packages: "
            + ", ".join(missing)
            + ". Run: python -m pip install -r requirements.txt"
        )
    ok("Required starter packages import successfully")


def check_data() -> None:
    data = ROOT / "data"
    required = [
        "employees.csv",
        "department_budgets.csv",
        "software_catalog.csv",
        "vendors.csv",
        "purchase_history.csv",
        "requests.json",
        "vendor_risk.json",
        "procurement_policy.md",
    ]
    missing = [name for name in required if not (data / name).is_file()]
    if missing:
        fail("Missing data files: " + ", ".join(missing))

    with (data / "employees.csv").open(encoding="utf-8", newline="") as f:
        employees = list(csv.DictReader(f))
    with (data / "department_budgets.csv").open(encoding="utf-8", newline="") as f:
        budgets = list(csv.DictReader(f))
    with (data / "vendors.csv").open(encoding="utf-8", newline="") as f:
        vendors = list(csv.DictReader(f))
    requests = json.loads((data / "requests.json").read_text(encoding="utf-8"))
    risk = json.loads((data / "vendor_risk.json").read_text(encoding="utf-8"))

    employee_ids = {r["employee_id"] for r in employees}
    departments = {r["department"] for r in budgets}
    vendor_names = {r["vendor_name"] for r in vendors}

    if any(r["requester_id"] not in employee_ids for r in requests):
        fail("At least one request references an unknown employee")
    if any(int(r["annual_software_budget_usd"]) - int(r["committed_usd"]) != int(r["available_usd"]) for r in budgets):
        fail("Budget arithmetic is inconsistent")
    if any(r["vendor_name"] not in vendor_names for r in requests):
        fail("At least one request references a vendor missing from vendors.csv")
    if any(r["vendor_name"] not in risk for r in requests):
        fail("At least one request vendor is missing from vendor_risk.json")
    if any(next(e for e in employees if e["employee_id"] == r["requester_id"])["department"] not in departments for r in requests):
        fail("At least one requester department is missing from department_budgets.csv")

    ok(f"Dataset integrity checks passed ({len(requests)} requests, {len(vendors)} vendors)")


def check_contract_and_evals() -> None:
    from src.contracts import EvidenceItem, ProcurementDecision

    sample = ProcurementDecision(
        request_id="REQ-CHECK",
        recommendation="Manual review",
        evidence=[EvidenceItem(source="setup", finding="Contract validation works")],
        next_step="Continue implementation",
    )
    if sample.request_id != "REQ-CHECK":
        fail("ProcurementDecision contract validation failed")

    cases = json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))
    request_ids = {
        r["request_id"]
        for r in json.loads((ROOT / "data" / "requests.json").read_text(encoding="utf-8"))
    }
    bad_refs = [c["request_id"] for c in cases if c["request_id"] not in request_ids]
    if bad_refs:
        fail("Public eval references unknown request IDs: " + ", ".join(bad_refs))
    ok(f"Output contract + {len(cases)} public evaluation cases are valid")


def check_mock_api() -> None:
    from fastapi.testclient import TestClient
    from mock_api.app import app

    client = TestClient(app)
    health = client.get("/health")
    if health.status_code != 200 or health.json().get("status") != "ok":
        fail("Mock API health endpoint failed")
    known = client.get("/vendor-risk/BrandBoard")
    if known.status_code != 200 or known.json().get("security_review_status") != "not_completed":
        fail("Mock API known-vendor check failed")
    outage = client.get("/vendor-risk/NimbusAI")
    if outage.status_code != 503:
        fail("Mock API simulated-outage check failed")
    ok("Mock vendor-risk API checks passed")


def check_tools_and_policy() -> None:
    from src.data_access import load_reference_date
    from src.tools import TOOLS

    deterministic = [name for name, spec in TOOLS.items() if spec.deterministic]
    if len(TOOLS) < 3 or not deterministic:
        fail("The copilot must expose at least 3 tools, at least 1 of them deterministic")
    ok(f"{len(TOOLS)} tools registered ({len(deterministic)} deterministic); policy reference date {load_reference_date()}")


def check_model_config() -> None:
    """A missing key is a warning, not a failure: the copilot then runs its deterministic checks only."""
    from src.llm import LLMError, LLMSettings

    try:
        settings = LLMSettings.from_env()
    except LLMError as exc:
        print(f"[WARN] No model configured - recommendations will be deterministic-only. {exc}")
    else:
        ok(f"Model provider configured: {settings.provider} / {settings.model}")


def main() -> None:
    print("Procurement Request Copilot - pre-flight\n")
    check_python()
    check_imports()
    check_data()
    check_contract_and_evals()
    check_mock_api()
    check_tools_and_policy()
    check_model_config()
    print("\nPRE-FLIGHT PASSED")
    print("Next: python run_local.py   (starts the mock vendor-risk API and the UI)")


if __name__ == "__main__":
    main()
