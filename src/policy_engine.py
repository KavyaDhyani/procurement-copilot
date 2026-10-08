"""Deterministic policy engine - procurement policy 2026.09 as code.

Pure functions over tool outputs: no I/O, no model, no clock. Everything a
reviewer must be able to rely on (required fields, budget, approval thresholds,
Security/Privacy/Legal triggers, review expiry, source conflicts) is decided
here. The model may *add* context (e.g. a data class it inferred from free
text), which can only make the outcome more conservative, never less.
"""
from __future__ import annotations

import re
from datetime import date

from src.data_access import name_key
from src.injection import scan_fields
from src.models import DATA_CLASSES, PolicyAssessment, ProcurementRequest, RuleHit, is_blank

# --- Policy constants (section numbers refer to data/procurement_policy.md) -------------

APPROVAL_ORDER = ["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]

# Section 4: (inclusive upper bound in USD, minimum business approvals)
APPROVAL_TIERS: list[tuple[float, list[str]]] = [
    (1_000, ["Manager"]),
    (10_000, ["Department Head", "Procurement"]),
    (25_000, ["Department Head", "Finance", "Procurement"]),
    (float("inf"), ["Department Head", "Finance", "CFO", "Procurement"]),
]
REVIEW_VALIDITY_DAYS = 365  # section 5
NEW_VENDOR_LEGAL_THRESHOLD_USD = 10_000  # section 7 ("$10,000 or more")

SECURITY_DATA_CLASSES = {
    "source_code": "source code access",
    "production_access": "production or cloud-account integration",
    "confidential_documents": "confidential documents",
    "employee_pii": "employee PII",
    "customer_pii": "customer PII",
    "personal_data": "personal data",
    "credentials": "credentials/secrets",
}
PII_CLASSES = {"employee_pii", "customer_pii", "personal_data"}

# Matched against the data-access level and integrations after "_"/"-" become spaces.
_DATA_CLASS_PATTERNS: dict[str, str] = {
    "source_code": r"source code|code repositor|\bgit\b|github|gitlab|bitbucket|codebase",
    "production_access": r"\bprod\b|production|cloud account|\baws\b|\bazure\b|\bgcp\b",
    "confidential_documents": r"confidential|restricted doc|privileged",
    "employee_pii": r"employee (pii|personal|data|records?)|\bhr\b|payroll|staff (pii|records?|data)",
    "customer_pii": r"(customer|client|consumer|patient) (pii|personal|data|records?)",
    "credentials": r"credential|secrets?\b|password|api key|private key|access token",
    "personal_data": r"\bpii\b|personal (data|information)|\bphi\b",
}


# --- Small helpers ------------------------------------------------------------------------

def review_age(review_date: str | None, reference_date: date) -> tuple[int | None, bool | None]:
    """(age in days, is_current) for a review date; (None, None) if absent or unparseable.

    A review is current for 365 days from its date. A date after the snapshot
    date is treated as not current: it cannot be verified as of the snapshot.
    """
    if is_blank(review_date):
        return None, None
    try:
        age = (reference_date - date.fromisoformat(str(review_date).strip())).days
    except ValueError:
        return None, None
    return age, 0 <= age <= REVIEW_VALIDITY_DAYS


def approval_tier(annual_cost_usd: float) -> tuple[str, list[str]]:
    lower = 0.0
    for upper, approvals in APPROVAL_TIERS:
        if annual_cost_usd <= upper:
            label = f"above ${lower:,.0f}" if upper == float("inf") else f"up to ${upper:,.0f}"
            return label, list(approvals)
        lower = upper
    raise AssertionError("unreachable")


def detect_data_classes(request: ProcurementRequest) -> list[str]:
    """Data classes evident from the structured fields. A floor the model can add to, not remove from."""
    text = " ".join([request.data_access_level or "", *(request.requested_integrations or [])])
    text = re.sub(r"[_\-/]+", " ", text).casefold()
    found = [cls for cls, pattern in _DATA_CLASS_PATTERNS.items() if re.search(pattern, text)]
    if "personal_data" in found and (set(found) & {"employee_pii", "customer_pii"}):
        found.remove("personal_data")
    return found


def _status(value: object) -> str:
    """Normalise a security-review status from either source to a comparable vocabulary."""
    key = re.sub(r"[_\-]+", " ", str(value or "")).strip().casefold()
    if key in {"approved", "current", "passed", "complete", "completed"}:
        return "approved"
    if key in {"expired", "lapsed"}:
        return "expired"
    if key in {"pending", "not completed", "in progress", "incomplete", "not started", "new"}:
        return "not_completed"
    if key in {"rejected", "failed", "denied"}:
        return "rejected"
    return "unknown"


def _ordered(approvals: set[str]) -> list[str]:
    return [a for a in APPROVAL_ORDER if a in approvals] + sorted(approvals - set(APPROVAL_ORDER))


# --- The engine ---------------------------------------------------------------------------

def assess(
    request: ProcurementRequest,
    budget: dict,
    catalog: dict,
    registry: dict,
    risk: dict,
    reference_date: date,
    model_data_classes: list[str] | tuple[str, ...] = (),
) -> PolicyAssessment:
    hits: list[RuleHit] = []
    approvals: set[str] = set()
    flags: list[str] = []
    missing: list[str] = []

    def hit(rule_id: str, section: str, finding: str, add_approvals: tuple[str, ...] = (), add_flags: tuple[str, ...] = ()) -> None:
        hits.append(RuleHit(rule_id=rule_id, policy_section=section, finding=finding,
                            adds_approvals=list(add_approvals), adds_flags=list(add_flags)))
        approvals.update(add_approvals)
        for flag in add_flags:
            if flag not in flags:
                flags.append(flag)

    cost = request.annual_cost_usd
    vendor = request.vendor_name

    # ---- Section 1: required request information
    if is_blank(request.requester_id) or budget.get("status") == "unknown_requester":
        missing.append("Requester and department could not be confirmed against the employee directory")
    if is_blank(request.product_name) or is_blank(vendor):
        missing.append("Product and/or vendor name is not provided")
    if cost is None:
        missing.append("Annual cost (or a reasonable annual estimate) is not provided")
    if request.user_count is None:
        missing.append("Number of users/licenses is not provided")
    if is_blank(request.business_justification):
        missing.append("Business purpose is not provided")
    if is_blank(request.data_access_level):
        missing.append("Intended data-access level is not specified")
    if request.requested_integrations is None:
        missing.append("Required integrations are not specified")
    if missing:
        detail = "; ".join(missing)
        if request.input_issues:
            detail += " (input problems: " + "; ".join(request.input_issues) + ")"
        hit("required_information", "Policy section 1",
            f"Request is not ready for approval - {detail}.", add_flags=("missing_information",))

    # ---- Section 2: budget
    status = budget.get("status")
    if status == "ok":
        available = budget["available_usd"]
        if budget["within_budget"]:
            hit("budget_check", "Policy section 2",
                f"Annual cost ${cost:,.2f} is within {budget['department']}'s available software budget "
                f"${available:,.2f} (this alone does not imply approval).")
        else:
            hit("budget_check", "Policy section 2",
                f"Annual cost ${cost:,.2f} exceeds {budget['department']}'s available software budget "
                f"${available:,.2f} by ${budget['shortfall_usd']:,.2f}; Finance budget-exception review required.",
                add_approvals=("Finance",), add_flags=("budget_insufficient",))
    elif status == "cost_missing":
        hit("budget_check", "Policy section 2", "Budget check could not be evaluated because no annual cost was given.")
    else:
        hit("budget_check", "Policy section 2",
            "Budget could not be verified: " + str(budget.get("detail", "no budget record for the requester's department"))
            + ". Routed to Finance rather than assuming budget is available.",
            add_approvals=("Finance",), add_flags=("budget_unverified",))

    # ---- Section 4: financial approval thresholds
    if cost is not None:
        label, tier_approvals = approval_tier(cost)
        hit("approval_threshold", "Policy section 4",
            f"Annual amount ${cost:,.2f} falls in the '{label}' tier: minimum approvals "
            + " + ".join(tier_approvals) + ".", add_approvals=tuple(tier_approvals))
    else:
        hit("approval_threshold", "Policy section 4",
            "Approval tier cannot be determined until an annual cost is provided.")

    # ---- Data classes: structured-field floor, plus anything the model inferred
    detected = detect_data_classes(request)
    from_model = [c for c in dict.fromkeys(model_data_classes) if c in DATA_CLASSES and c not in detected]
    data_classes = detected + from_model

    # ---- Section 5: security review
    for cls in data_classes:
        source = " (inferred by the AI from request text - reviewer to confirm)" if cls in from_model else ""
        hit(f"security_data_class:{cls}", "Policy section 5",
            f"Request involves {SECURITY_DATA_CLASSES[cls]}{source}; Security review required.",
            add_approvals=("Security",), add_flags=("security_review_required",))

    registry_found = bool(registry.get("found"))
    registry_status = _status(registry.get("security_status")) if registry_found else "unknown"
    risk_outcome = risk.get("outcome")
    risk_status = _status(risk.get("security_review_status")) if risk_outcome == "ok" else "unknown"
    evidence_unavailable = False
    evidence_conflict = False

    if not is_blank(vendor):
        # Internal registry
        if not registry_found:
            hit("vendor_registry", "Policy section 5",
                f"Vendor '{vendor}' is not in the internal vendor registry: no security assessment on file.",
                add_approvals=("Security",), add_flags=("security_review_required",))
        elif registry_status != "approved" or registry.get("review_current") is None:
            hit("vendor_registry", "Policy section 5",
                f"Registry shows security status '{registry.get('security_status') or 'blank'}' with review date "
                f"'{registry.get('security_review_date') or 'none'}': assessment missing or not completed.",
                add_approvals=("Security",), add_flags=("security_review_required",))
        elif registry.get("review_current") is False:
            hit("vendor_registry", "Policy section 5",
                f"Registry security review dated {registry['security_review_date']} is {registry['review_age_days']} days old "
                f"at the {reference_date.isoformat()} reference date (valid for {REVIEW_VALIDITY_DAYS} days): expired.",
                add_approvals=("Security",), add_flags=("security_review_required", "vendor_review_expired"))

        # External vendor-risk service (sections 5 and 10)
        if risk_outcome in ("unavailable", "invalid_response"):
            evidence_unavailable = True
            hit("vendor_risk_service", "Policy section 10",
                f"Vendor-risk service could not be checked ({risk.get('detail') or risk_outcome}). "
                "No favourable status is inferred; the vendor's security posture is unverified.",
                add_approvals=("Security",), add_flags=("vendor_risk_unavailable", "security_review_required"))
        elif risk_outcome == "not_found":
            hit("vendor_risk_service", "Policy section 5",
                f"Vendor-risk service has no record for '{vendor}': security assessment missing.",
                add_approvals=("Security",), add_flags=("security_review_required",))
        elif risk_outcome == "ok":
            expired = risk_status == "expired" or risk.get("review_current") is False
            if expired:
                hit("vendor_risk_service", "Policy section 5",
                    f"Vendor-risk service reports status '{risk.get('security_review_status')}' with last review "
                    f"{risk.get('last_review_date') or 'none'} ({risk.get('review_age_days')} days before the reference date): expired.",
                    add_approvals=("Security",), add_flags=("security_review_required", "vendor_review_expired"))
            elif risk_status != "approved" or risk.get("review_current") is None:
                hit("vendor_risk_service", "Policy section 5",
                    f"Vendor-risk service reports security review '{risk.get('security_review_status')}' "
                    f"(last review: {risk.get('last_review_date') or 'none'}): assessment missing or not completed.",
                    add_approvals=("Security",), add_flags=("security_review_required",))

        # Disagreement between the two sources (section 5)
        if registry_found and risk_outcome == "ok":
            conflicts = []
            if "unknown" not in (registry_status, risk_status) and registry_status != risk_status:
                conflicts.append(f"status: registry '{registry.get('security_status')}' vs service '{risk.get('security_review_status')}'")
            reg_date, svc_date = registry.get("security_review_date"), risk.get("last_review_date")
            if not is_blank(reg_date) and not is_blank(svc_date) and str(reg_date).strip() != str(svc_date).strip():
                conflicts.append(f"review date: registry {reg_date} vs service {svc_date}")
            if conflicts:
                evidence_conflict = True
                hit("vendor_source_conflict", "Policy section 5",
                    "Internal registry and vendor-risk service disagree (" + "; ".join(conflicts)
                    + "). Neither is silently preferred; Security to resolve.",
                    add_approvals=("Security",), add_flags=("conflicting_vendor_evidence", "security_review_required"))

    # ---- Section 6: privacy review
    pii = [c for c in data_classes if c in PII_CLASSES]
    outside_region = risk_outcome == "ok" and risk.get("stores_data_outside_region") is True
    if pii:
        hit("privacy_pii", "Policy section 6",
            "Tool will process " + ", ".join(SECURITY_DATA_CLASSES[c] for c in pii) + "; Privacy review required.",
            add_approvals=("Privacy",), add_flags=("privacy_review_required",))
    if outside_region and data_classes:
        hit("privacy_cross_region", "Policy sections 6 and 7",
            "Vendor-risk service reports data stored outside the operating region and the request involves sensitive data ("
            + ", ".join(data_classes) + "): Privacy review and Legal review of the cross-region issue required.",
            add_approvals=("Privacy", "Legal"), add_flags=("privacy_review_required", "legal_review_required"))
    elif outside_region and is_blank(request.data_access_level):
        hit("privacy_cross_region", "Policy section 6",
            "Vendor stores data outside the operating region; Privacy review will be required if the "
            "(currently unspecified) data-access level includes sensitive data.")

    # ---- Section 7: legal review
    if not is_blank(vendor):
        is_new = (not registry_found) or name_key(registry.get("procurement_status")) in {"new", "", "pending", "unknown"}
        terms = name_key(registry.get("legal_terms_status")) if registry_found else ""
        if is_new and cost is not None and cost >= NEW_VENDOR_LEGAL_THRESHOLD_USD:
            hit("legal_new_vendor", "Policy section 7",
                f"Vendor is new (registry status: {registry.get('procurement_status') or 'not registered'}) and annual spend "
                f"${cost:,.2f} is at or above ${NEW_VENDOR_LEGAL_THRESHOLD_USD:,}; Legal review required.",
                add_approvals=("Legal",), add_flags=("legal_review_required",))
        if terms not in {"approved", "standard"}:
            hit("legal_terms", "Policy section 7",
                f"Legal terms are not approved/standard (registry: '{registry.get('legal_terms_status') or 'no record'}'); Legal review required.",
                add_approvals=("Legal",), add_flags=("legal_review_required",))

    # ---- Section 3: existing software / overlap (facts only; whether it matters is a judgement)
    overlap_candidates: list[str] = []
    same_vendor_products: list[str] = []
    for match in catalog.get("matches", []):
        reasons = set(match.get("match_reasons", []))
        label = f"{match['product_name']} ({match['software_id']})"
        other_vendor = name_key(match.get("vendor_name")) != name_key(vendor)
        if "same_product" in reasons or ("same_category" in reasons and other_vendor):
            overlap_candidates.append(label)
        elif "same_vendor" in reasons:
            same_vendor_products.append(label)
        # Section 8: limited-use approvals do not extend to new data classes
        if "same_vendor" in reasons and "limited" in name_key(match.get("status")) and data_classes:
            hit("limited_use_approval", "Policy section 8",
                f"{label} is '{match.get('status')}' ({match.get('notes') or 'restrictions apply'}); the existing approval "
                "does not authorise " + ", ".join(SECURITY_DATA_CLASSES[c] for c in data_classes) + ".")
    if overlap_candidates:
        hit("existing_tool_overlap", "Policy section 3",
            "Approved catalog already contains " + ", ".join(overlap_candidates)
            + " in the same category or as the same product; consider the existing option first "
            "(overlap is not an automatic rejection).",
            add_flags=("existing_tool_overlap",))
    elif same_vendor_products:
        hit("existing_vendor_product", "Policy section 3",
            "Catalog already contains " + ", ".join(same_vendor_products)
            + " from the same vendor; confirm whether this is an expansion/add-on or duplicates existing capacity.")

    # ---- Section 9: instruction-like text inside business data
    suspicious = scan_fields({
        "request.business_justification": request.business_justification,
        "request.product_name": request.product_name,
        "request.vendor_name": request.vendor_name,
        "request.category": request.category,
        "request.data_access_level": request.data_access_level,
        "request.urgency": request.urgency,
        "request.requested_integrations": " | ".join(request.requested_integrations or []),
        "vendor_registry.notes": registry.get("notes"),
        "vendor_risk_service.notes": risk.get("notes"),
        "software_catalog.notes": " | ".join(str(m.get("notes") or "") for m in catalog.get("matches", [])),
    })
    if suspicious:
        hit("untrusted_instruction_text", "Policy section 9",
            "Business data contains instruction-like text that was ignored: "
            + "; ".join(f"{s['field']}: \"{s['snippet']}\"" for s in suspicious[:3]) + ".",
            add_flags=("prompt_injection_detected",))

    # ---- Which next actions are consistent with the rules above
    specialist = bool(approvals & {"Security", "Privacy", "Legal"}) or bool({"budget_insufficient", "budget_unverified"} & set(flags))
    if missing:
        allowed = ["request_clarification"]
    elif evidence_unavailable or evidence_conflict:
        allowed = ["manual_review"]
    elif specialist:
        allowed = ["route_for_reviews", "review_existing_tool_first"]
    else:
        allowed = ["proceed_to_standard_approval", "review_existing_tool_first"]

    return PolicyAssessment(
        reference_date=reference_date.isoformat(),
        data_classes=data_classes,
        data_classes_from_model_only=from_model,
        missing_information=missing,
        required_approvals=_ordered(approvals),
        risk_flags=flags,
        rule_hits=hits,
        overlap_candidates=overlap_candidates + same_vendor_products,
        evidence_unavailable=evidence_unavailable,
        evidence_conflict=evidence_conflict,
        specialist_review=specialist,
        allowed_actions=allowed,
        default_action=allowed[0],
    )
