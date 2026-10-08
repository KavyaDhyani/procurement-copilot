"""The copilot's tools and the evidence ledger they write to.

Five tools, one registry. Four are deterministic lookups/computations over the
data snapshot; one calls the external vendor-risk service. Every execution is
recorded as a ledger entry (E1, E2, ...) - the only thing a finding may cite.

The agent chooses which tools to run and with what arguments. Policy makes four
checks mandatory, so `evaluate_policy_rules` always works from *canonical* calls
whose arguments come from the request record itself, never from the model. If
the agent skipped or mis-addressed a check, the harness runs it and the ledger
says so (`requested_by="harness"`).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from src import data_access, policy_engine
from src.config import Settings
from src.data_access import name_key
from src.models import DATA_CLASSES, PolicyAssessment, ProcurementRequest, ToolCall
from src.vendor_client import fetch_vendor_risk


@dataclass
class RunContext:
    """Per-request state: the request, where to read data, and the evidence ledger."""

    request: ProcurementRequest
    settings: Settings
    reference_date: date
    ledger: list[ToolCall] = field(default_factory=list)
    assessment: PolicyAssessment | None = None
    model_data_classes: set[str] = field(default_factory=set)
    _cache: dict[str, ToolCall] = field(default_factory=dict)

    def call(self, tool: str, arguments: dict[str, Any], requested_by: str = "agent") -> ToolCall:
        """Run a tool once per distinct (tool, arguments); repeat requests return the same ledger entry."""
        spec = TOOLS.get(tool)
        if spec is None:
            return self._record(tool, arguments, requested_by, "unknown_tool",
                                {"error": f"Unknown tool '{tool}'"}, f"Unknown tool '{tool}' was requested.", None, 0.0)
        try:
            args = spec.normalise(arguments or {})
        except (TypeError, ValueError) as exc:
            return self._record(tool, arguments or {}, requested_by, "invalid_arguments",
                                {"error": str(exc)}, f"{tool} was called with invalid arguments: {exc}", None, 0.0)
        key = tool + ":" + json.dumps(args, sort_keys=True, default=str)
        if key in self._cache:
            return self._cache[key]
        started = time.perf_counter()
        status, output, summary, reference = spec.run(self, **args)
        entry = self._record(tool, args, requested_by, status, output, summary, reference,
                             (time.perf_counter() - started) * 1000)
        self._cache[key] = entry
        return entry

    def _record(self, tool, arguments, requested_by, status, output, summary, reference, elapsed_ms) -> ToolCall:
        entry = ToolCall(evidence_id=f"E{len(self.ledger) + 1}", tool=tool, arguments=arguments,
                         requested_by=requested_by, status=status, output=output, summary=summary,
                         reference=reference, elapsed_ms=round(elapsed_ms, 1))
        self.ledger.append(entry)
        return entry


# --- Argument helpers -----------------------------------------------------------------------

def _str(value: object, name: str, required: bool = True) -> str | None:
    if value is None or not str(value).strip():
        if required:
            raise ValueError(f"'{name}' is required")
        return None
    return " ".join(str(value).split())[:200]


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError("'annual_cost_usd' must be a number or null")


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _to_float(value: object) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


# --- Tool 1: budget check (deterministic) ---------------------------------------------------

def _budget_args(args: dict) -> dict:
    return {"requester_id": _str(args.get("requester_id"), "requester_id", required=False),
            "annual_cost_usd": _num(args.get("annual_cost_usd"))}


def _check_budget(ctx: RunContext, requester_id: str | None, annual_cost_usd: float | None):
    data_dir = ctx.settings.data_dir
    employee = data_access.find_employee(requester_id, data_dir)
    if employee is None:
        return ("unknown_requester", {"status": "unknown_requester", "requester_id": requester_id,
                                      "detail": f"no employee record for requester '{requester_id}'"},
                f"Requester '{requester_id}' is not in the employee directory; department and budget cannot be determined.",
                "employees.csv")
    manager = data_access.find_employee(employee.get("manager_id"), data_dir)
    out: dict[str, Any] = {
        "requester_id": employee["employee_id"], "requester_name": employee["name"],
        "department": employee["department"], "manager_id": employee.get("manager_id") or None,
        "manager_name": manager["name"] if manager else None, "annual_cost_usd": annual_cost_usd,
    }
    budget = data_access.find_budget(employee["department"], data_dir)
    available = _to_float(budget["available_usd"]) if budget else None
    reference = f"department_budgets.csv:{employee['department']}"
    if available is None:
        out |= {"status": "no_budget_record", "detail": f"no budget record for department '{employee['department']}'"}
        return ("no_budget_record", out,
                f"{employee['name']} is in {employee['department']}, which has no software-budget record; budget cannot be verified.",
                reference)
    out["available_usd"] = available
    if annual_cost_usd is None:
        out |= {"status": "cost_missing", "within_budget": None}
        return ("cost_missing", out,
                f"{employee['department']} has {_money(available)} available software budget, but the request has no annual cost to compare.",
                reference)
    within = annual_cost_usd <= available
    out |= {"status": "ok", "within_budget": within, "shortfall_usd": 0.0 if within else round(annual_cost_usd - available, 2)}
    summary = (f"{employee['department']} has {_money(available)} available software budget; the request costs "
               f"{_money(annual_cost_usd)}/yr - " + ("within budget." if within else f"over budget by {_money(out['shortfall_usd'])}."))
    return "ok", out, summary, reference


# --- Tool 2: software catalog search (deterministic) ----------------------------------------

_STOPWORDS = {"the", "and", "for", "tool", "tools", "software", "team", "teams", "app", "new", "with", "need", "needs", "pro",
              "enterprise", "business", "platform", "solution", "service", "services", "add", "pack"}


def _catalog_args(args: dict) -> dict:
    keywords = args.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    cleaned = sorted({name_key(k) for k in keywords if isinstance(k, str) and len(name_key(k)) >= 3} - _STOPWORDS)[:8]
    return {"keywords": cleaned}


def _search_catalog(ctx: RunContext, keywords: list[str]):
    """Structural matches (same product / vendor / category as the request) are always included,
    so one search answers policy section 3 whatever keywords the agent chose."""
    data_dir = ctx.settings.data_dir
    product_name, vendor_name, category = ctx.request.product_name, ctx.request.vendor_name, ctx.request.category
    purchases = data_access.load_purchase_history(data_dir)
    matches = []
    for row in data_access.load_software_catalog(data_dir):
        reasons: list[str] = []
        row_product = name_key(row["product_name"])
        if product_name and (row_product == name_key(product_name)):
            reasons.append("same_product")
        if vendor_name and name_key(row["vendor_name"]) == name_key(vendor_name):
            reasons.append("same_vendor")
        if category and name_key(row["category"]) == name_key(category):
            reasons.append("same_category")
        haystack = name_key(" ".join([row["product_name"], row["category"], row["vendor_name"], row.get("notes", "")]))
        reasons += [f"keyword:{k}" for k in keywords if re.search(rf"\b{re.escape(k)}", haystack)]
        if not reasons:
            continue
        last = max((p for p in purchases if name_key(p["product_name"]) == row_product
                    or name_key(p["vendor_name"]) == name_key(row["vendor_name"])),
                   key=lambda p: p["purchase_date"], default=None)
        matches.append({
            "software_id": row["software_id"], "product_name": row["product_name"], "category": row["category"],
            "vendor_name": row["vendor_name"], "status": row["status"],
            "annual_cost_usd": _to_float(row["annual_cost_usd"]), "licensed_seats": _to_float(row["licensed_seats"]),
            "scope": row["scope"], "notes": row.get("notes") or None, "match_reasons": reasons,
            "last_purchase": (f"{last['purchase_id']} on {last['purchase_date']} by {last['department']}, "
                              f"${_to_float(last['annual_amount_usd']) or 0:,.0f}/yr ({last.get('notes') or 'no notes'})" if last else None),
        })

    def rank(m: dict) -> tuple:
        structural = [r for r in m["match_reasons"] if not r.startswith("keyword:")]
        return (not structural, "same_product" not in structural, "same_vendor" not in structural, -len(m["match_reasons"]))

    matches = sorted(matches, key=rank)[:6]
    out = {"match_count": len(matches), "matches": matches}
    if not matches:
        return ("ok", out, "No approved catalog entry matches the requested product, vendor, category or search keywords.",
                "software_catalog.csv")
    described = "; ".join(
        f"{m['product_name']} ({m['software_id']}, {m['category']}, {m['status']}, {(m['licensed_seats'] or 0):.0f} seats, "
        f"scope {m['scope']}; matched on {', '.join(m['match_reasons'])})" for m in matches)
    return ("ok", out, f"Approved catalog has {len(matches)} related entr{'y' if len(matches) == 1 else 'ies'}: {described}.",
            "software_catalog.csv:" + ",".join(m["software_id"] for m in matches))


# --- Tool 3: internal vendor registry (deterministic) ---------------------------------------

def _vendor_args(args: dict) -> dict:
    return {"vendor_name": _str(args.get("vendor_name"), "vendor_name")}


def _lookup_vendor_registry(ctx: RunContext, vendor_name: str):
    row = data_access.find_vendor(vendor_name, ctx.settings.data_dir)
    if row is None:
        return ("not_found", {"found": False, "vendor_name": vendor_name},
                f"'{vendor_name}' is not in the internal vendor registry.", "vendors.csv")
    age, current = policy_engine.review_age(row.get("security_review_date"), ctx.reference_date)
    out = {"found": True, "vendor_id": row["vendor_id"], "vendor_name": row["vendor_name"],
           "procurement_status": row.get("procurement_status") or None, "security_status": row.get("security_status") or None,
           "security_review_date": row.get("security_review_date") or None, "review_age_days": age, "review_current": current,
           "legal_terms_status": row.get("legal_terms_status") or None, "notes": row.get("notes") or None}
    when = (f"reviewed {out['security_review_date']} ({age} days before the {ctx.reference_date.isoformat()} reference date, "
            f"{'current' if current else 'EXPIRED'})" if age is not None else "no security review date on file")
    return ("ok", out,
            f"Registry {row['vendor_id']}: {row['vendor_name']} - procurement status {out['procurement_status']}, security status "
            f"{out['security_status']}, {when}, legal terms {out['legal_terms_status']}.", f"vendors.csv:{row['vendor_id']}")


# --- Tool 4: external vendor-risk service (HTTP) --------------------------------------------

def _get_vendor_risk(ctx: RunContext, vendor_name: str):
    result = fetch_vendor_risk(vendor_name, ctx.settings)
    out: dict[str, Any] = {"outcome": result.outcome, "http_status": result.http_status}
    if result.outcome != "ok":
        out["detail"] = result.detail
        summary = {
            "not_found": f"Vendor-risk service has no record for '{vendor_name}' (HTTP 404).",
            "unavailable": f"Vendor-risk service was unavailable for '{vendor_name}' after {result.attempts} attempt(s)"
                           f"{f' (HTTP {result.http_status})' if result.http_status else ''}: {result.detail}. Status NOT verified.",
            "invalid_response": f"Vendor-risk service returned an unusable response for '{vendor_name}'. Status NOT verified.",
        }[result.outcome]
        return result.outcome, out, summary, result.endpoint
    record = result.record or {}
    age, current = policy_engine.review_age(record.get("last_review_date"), ctx.reference_date)
    out |= {"risk_level": record.get("risk_level"), "security_review_status": record.get("security_review_status"),
            "last_review_date": record.get("last_review_date"), "review_age_days": age, "review_current": current,
            "processes_personal_data": record.get("processes_personal_data"),
            "stores_data_outside_region": record.get("stores_data_outside_region"), "notes": record.get("notes")}
    when = f"last review {out['last_review_date']} ({age} days old, {'current' if current else 'EXPIRED'})" if age is not None else "no review date"
    return ("ok", out,
            f"Vendor-risk service: {vendor_name} - risk level {out['risk_level']}, security review {out['security_review_status']}, "
            f"{when}, processes personal data: {out['processes_personal_data']}, stores data outside region: "
            f"{out['stores_data_outside_region']}.", result.endpoint)


# --- Tool 5: policy rule evaluation (deterministic) -----------------------------------------

def _policy_args(args: dict) -> dict:
    classes = args.get("data_classes") or []
    if isinstance(classes, str):
        classes = [classes]
    return {"data_classes": sorted({c for c in classes if c in DATA_CLASSES})}


def canonical_evidence(ctx: RunContext) -> dict[str, ToolCall | None]:
    """The four mandatory checks, addressed from the request record (never from model-supplied arguments)."""
    req = ctx.request
    vendor = req.vendor_name
    catalog = next((e for e in ctx.ledger if e.tool == "search_software_catalog" and e.status == "ok"), None)
    return {
        "budget": ctx.call("check_budget", {"requester_id": req.requester_id, "annual_cost_usd": req.annual_cost_usd}, "harness"),
        "catalog": catalog or ctx.call("search_software_catalog", {"keywords": []}, "harness"),
        "registry": ctx.call("lookup_vendor_registry", {"vendor_name": vendor}, "harness") if vendor else None,
        "risk": ctx.call("get_vendor_risk", {"vendor_name": vendor}, "harness") if vendor else None,
    }


def run_policy_engine(ctx: RunContext, data_classes: list[str] | tuple[str, ...] = ()) -> PolicyAssessment:
    # Model-inferred data classes accumulate across calls: a later call can add a class but never drop one.
    ctx.model_data_classes |= set(data_classes)
    evidence = canonical_evidence(ctx)
    ctx.assessment = policy_engine.assess(
        ctx.request,
        budget=evidence["budget"].output,
        catalog=evidence["catalog"].output,
        registry=evidence["registry"].output if evidence["registry"] else {"found": False},
        risk=evidence["risk"].output if evidence["risk"] else {"outcome": "not_checked"},
        reference_date=ctx.reference_date,
        model_data_classes=sorted(ctx.model_data_classes),
    )
    return ctx.assessment


def _evaluate_policy(ctx: RunContext, data_classes: list[str]):
    a = run_policy_engine(ctx, data_classes)
    out = {
        "reference_date": a.reference_date, "data_classes": a.data_classes,
        "missing_information": a.missing_information, "required_approvals": a.required_approvals,
        "risk_flags": a.risk_flags,
        # The model is told that instruction-like text was found, not shown it again; humans see the quote in the evidence.
        "rules": [{"rule": h.rule_id, "section": h.policy_section,
                   "finding": ("Instruction-like text was detected in business data and ignored."
                               if h.rule_id == "untrusted_instruction_text" else h.finding)} for h in a.rule_hits],
    }
    summary = (f"Policy rules ({a.reference_date} snapshot): approvals required - {', '.join(a.required_approvals) or 'none determinable yet'}; "
               f"risk flags - {', '.join(a.risk_flags) or 'none'}; missing information - {len(a.missing_information)} item(s).")
    return "ok", out, summary, "procurement_policy.md sections 1-10"


# --- Registry -------------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, dict]
    normalise: Callable[[dict], dict]
    run: Callable[..., tuple[str, dict, str, str | None]]
    deterministic: bool = True


_NULLABLE_STR = {"type": ["string", "null"]}

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in [
    ToolSpec("check_budget",
             "Compare the annual cost with the requester's department available software budget; also returns department and manager.",
             {"requester_id": _NULLABLE_STR, "annual_cost_usd": {"type": ["number", "null"]}},
             _budget_args, _check_budget),
    ToolSpec("search_software_catalog",
             "Search the approved software catalog. Always returns entries for the request's own product, vendor and category; keywords naming the capability NEEDED also find other existing tools that could meet it.",
             {"keywords": {"type": "array", "items": {"type": "string"}}},
             _catalog_args, _search_catalog),
    ToolSpec("lookup_vendor_registry",
             "Internal vendor registry: onboarding status, security status and review date, legal terms status.",
             {"vendor_name": {"type": "string"}}, _vendor_args, _lookup_vendor_registry),
    ToolSpec("get_vendor_risk",
             "External vendor-risk service: security review status/date, risk level, personal-data and data-residency facts. May be unavailable.",
             {"vendor_name": {"type": "string"}}, _vendor_args, _get_vendor_risk, deterministic=False),
    ToolSpec("evaluate_policy_rules",
             "Deterministic policy engine: required fields, budget, approval thresholds, Security/Privacy/Legal triggers, review expiry, source conflicts. Pass the sensitive data classes involved.",
             {"data_classes": {"type": "array", "items": {"type": "string", "enum": list(DATA_CLASSES)}}},
             _policy_args, _evaluate_policy),
]}

def tool_request_schema() -> dict:
    """JSON schema for one tool request: a tagged union over the registry (used for strict structured output)."""
    return {"anyOf": [
        {"type": "object",
         "properties": {"tool": {"type": "string", "enum": [spec.name]}, **spec.properties},
         "required": ["tool", *spec.properties], "additionalProperties": False}
        for spec in TOOLS.values()]}


def tool_catalog_text() -> str:
    return "\n".join(f"- {spec.name}: {spec.description}" for spec in TOOLS.values())
