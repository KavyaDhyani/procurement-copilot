"""Internal data models shared by the tools, policy engine, agents and UI.

`src/contracts.py` remains the external output contract; these are the richer
internal records that the contract is assembled from.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts import ProcurementDecision

Action = Literal[
    "request_clarification",
    "manual_review",
    "review_existing_tool_first",
    "route_for_reviews",
    "proceed_to_standard_approval",
]

ACTION_LABELS: dict[str, str] = {
    "request_clarification": "Request clarification from the requester",
    "manual_review": "Manual review required - evidence unavailable or conflicting",
    "review_existing_tool_first": "Review existing tool before purchasing",
    "route_for_reviews": "Route for required reviews before approval",
    "proceed_to_standard_approval": "Proceed to standard approval",
}

DATA_CLASSES = (
    "source_code",
    "production_access",
    "confidential_documents",
    "employee_pii",
    "customer_pii",
    "personal_data",
    "credentials",
)

OVERLAP_LEVELS = (
    "none",
    "expansion_or_addon",
    "alternative_gap_stated",
    "alternative_no_gap",
    "likely_duplicate",
)

_UNKNOWN = {"", "unknown", "tbd", "tbc", "n/a", "na", "null", "?", "not sure", "unsure", "unspecified"}


def is_blank(value: object) -> bool:
    return value is None or str(value).strip().casefold() in _UNKNOWN


def _parse_amount(value: object) -> tuple[float | None, str | None]:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and is_blank(value)):
        return None, None
    try:
        amount = float(re.sub(r"[,$\s]|usd", "", str(value), flags=re.IGNORECASE))
    except ValueError:
        return None, f"annual_cost_usd is not a number: {str(value)[:40]!r}"
    if amount != amount or amount < 0 or amount == float("inf"):
        return None, f"annual_cost_usd is not a valid non-negative amount: {str(value)[:40]!r}"
    return amount, None


def _parse_count(value: object) -> tuple[int | None, str | None]:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and is_blank(value)):
        return None, None
    try:
        count = float(str(value).replace(",", ""))
    except ValueError:
        return None, f"user_count is not a number: {str(value)[:40]!r}"
    if count <= 0 or count != int(count):
        return None, f"user_count is not a positive whole number: {str(value)[:40]!r}"
    return int(count), None


def _text(value: object) -> str | None:
    return None if value is None or not str(value).strip() else str(value).strip()


class ProcurementRequest(BaseModel):
    """A purchase request, parsed leniently: bad or absent fields become None, never an exception."""

    model_config = ConfigDict(extra="ignore")

    request_id: str
    requester_id: str | None = None
    product_name: str | None = None
    vendor_name: str | None = None
    category: str | None = None
    annual_cost_usd: float | None = None
    user_count: int | None = None
    business_justification: str | None = None
    data_access_level: str | None = None
    requested_integrations: list[str] | None = None
    urgency: str | None = None
    input_issues: list[str] = Field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict) -> "ProcurementRequest":
        cost, cost_issue = _parse_amount(raw.get("annual_cost_usd"))
        users, users_issue = _parse_count(raw.get("user_count"))
        integrations = raw.get("requested_integrations")
        if isinstance(integrations, str):
            integrations = [integrations] if integrations.strip() else []
        elif isinstance(integrations, (list, tuple)):
            integrations = [str(i).strip() for i in integrations if str(i).strip()]
        else:
            integrations = None
        return cls(
            request_id=str(raw.get("request_id") or "UNKNOWN"),
            requester_id=_text(raw.get("requester_id")),
            product_name=_text(raw.get("product_name")),
            vendor_name=_text(raw.get("vendor_name")),
            category=_text(raw.get("category")),
            annual_cost_usd=cost,
            user_count=users,
            business_justification=_text(raw.get("business_justification")),
            data_access_level=_text(raw.get("data_access_level")),
            requested_integrations=integrations,
            urgency=_text(raw.get("urgency")),
            input_issues=[i for i in (cost_issue, users_issue) if i],
        )


class ToolCall(BaseModel):
    """One entry in the evidence ledger. Every finding shown to a human traces back to one of these."""

    evidence_id: str
    tool: str
    arguments: dict[str, Any]
    requested_by: Literal["agent", "harness"]
    status: str
    output: dict[str, Any]
    summary: str
    reference: str | None = None
    elapsed_ms: float = 0.0


class RuleHit(BaseModel):
    rule_id: str
    policy_section: str
    finding: str
    adds_approvals: list[str] = Field(default_factory=list)
    adds_flags: list[str] = Field(default_factory=list)


class PolicyAssessment(BaseModel):
    """Output of the deterministic policy engine. The model can read this but cannot weaken it."""

    reference_date: str
    data_classes: list[str] = Field(default_factory=list)
    data_classes_from_model_only: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    rule_hits: list[RuleHit] = Field(default_factory=list)
    overlap_candidates: list[str] = Field(default_factory=list)
    evidence_unavailable: bool = False
    evidence_conflict: bool = False
    specialist_review: bool = False
    allowed_actions: list[str] = Field(default_factory=list)
    default_action: str = "manual_review"


class GuardrailEvent(BaseModel):
    kind: str
    detail: str


class Usage(BaseModel):
    llm_calls: int = 0
    llm_retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_ms: float = 0.0
    tool_ms: float = 0.0
    rate_limit_wait_ms: float = 0.0


class AnalysisResult(BaseModel):
    """Everything a run produced: the contract decision plus the trace behind it."""

    decision: ProcurementDecision
    architecture: str
    action: str
    model_proposed_action: str | None = None
    need_summary: str | None = None
    rationale: str | None = None
    overlap_level: str | None = None
    assessment: PolicyAssessment
    ledger: list[ToolCall] = Field(default_factory=list)
    guardrail_events: list[GuardrailEvent] = Field(default_factory=list)
    stages: list[dict[str, Any]] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    degraded: bool = False
    model: str | None = None
