"""Turns the policy engine's results and the model's draft into the output contract.

This is where the "AI recommends, code decides the rules, humans approve" split
is enforced, identically for both architectures:

- approvals, policy risk flags and missing fields come from the policy engine;
  the model's draft can add an overlap or injection flag but can remove nothing;
- the model's proposed action is accepted only if it is consistent with the
  engine's results, otherwise it is overridden and the override is recorded;
- a model-written finding is kept only if it cites real ledger entries and every
  figure or date in it appears in those entries;
- human review is always required (policy section 11).

With no model draft at all (provider down, quota exhausted, no key) the same
function still returns a complete, conservative, clearly labelled decision.
"""
from __future__ import annotations

import json
import re

from src.agents.common import ModelDraft
from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.data_access import name_key
from src.models import ACTION_LABELS, AnalysisResult, GuardrailEvent, PolicyAssessment, ToolCall, Usage
from src.tools import TOOLS, RunContext

_OVERLAP_FLAG_LEVELS = {"alternative_gap_stated", "alternative_no_gap", "likely_duplicate"}
_POLICY_CONSTANTS = {"365", "1000", "10000", "25000"}
_NUMBER = re.compile(r"\d{4}-\d{2}-\d{2}|\d[\d,]*(?:\.\d+)?")
# Claims that the copilot (or anyone) has already approved/bought the thing under review.
_APPROVAL_CLAIM = re.compile(
    r"\b(i|we)\s+(have\s+|hereby\s+)?approv(e|ed)\b"
    r"|\b(request|purchase|spend|order)\s+(is|has been|was|is now|is hereby|is already)\s+(\w+[- ])?approved\b"
    r"|\bpurchase (has been|was) (made|completed|placed)\b"
    r"|\bno (human|further) (review|approval) (is )?(needed|required)\b",
    re.IGNORECASE)


def _numeric_tokens(text: str) -> set[str]:
    """Figures and dates worth verifying: ISO dates and numbers of three or more digits, normalised."""
    tokens: set[str] = set()
    for raw in _NUMBER.findall(text):
        if "-" in raw:
            tokens |= {raw, raw[:4]}
            continue
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        normalised = str(int(value)) if value == int(value) else f"{value:.2f}".rstrip("0")
        if len(normalised.replace(".", "")) >= 3:
            tokens.add(normalised)
    return tokens


def _entry_text(entry: ToolCall) -> str:
    return json.dumps([entry.arguments, entry.output], default=str) + " " + entry.summary


def _business_text(ctx: RunContext) -> str:
    """All untrusted free text the model saw: request fields plus notes returned by tools."""
    parts = [json.dumps(ctx.request.model_dump(), default=str)]
    parts += [_entry_text(e) for e in ctx.ledger if e.tool != "evaluate_policy_rules"]
    return " ".join(" ".join(parts).split()).casefold()


def _catalog_products(ctx: RunContext) -> dict[str, str]:
    return {name_key(m["product_name"]): m["product_name"]
            for e in ctx.ledger if e.tool == "search_software_catalog"
            for m in e.output.get("matches", [])}


def _fallback_rationale(action: str, a: PolicyAssessment) -> str:
    if action == "request_clarification":
        return f"{len(a.missing_information)} required item(s) are missing, so the request cannot be assessed for approval yet."
    if action == "manual_review":
        return "Vendor evidence is unavailable or conflicting, so the vendor's security status cannot be relied on without a human check."
    if action == "route_for_reviews":
        specialists = [x for x in a.required_approvals if x in ("Security", "Privacy", "Legal")]
        reasons = (["a Finance budget exception"] if {"budget_insufficient", "budget_unverified"} & set(a.risk_flags) else [])
        reasons += [f"{' / '.join(specialists)} review"] if specialists else []
        return "Policy requires " + " and ".join(reasons) + " before this can be approved."
    return "No specialist review trigger was found; only the standard approvers for this amount are required."


def _next_step(action: str, a: PolicyAssessment, requester: str, existing: list[str], missing: list[str]) -> str:
    approvers = ", ".join(a.required_approvals) or "the approvers determined once the request is complete"
    if action == "request_clarification":
        return (f"Return the request to {requester} to supply: " + "; ".join(missing)
                + ". Re-run the review once provided. No approval may be requested until then.")
    if action == "manual_review":
        return ("Escalate to Security for manual verification of the vendor's security status; do not treat the vendor as "
                f"cleared. Human sign-off is then required from: {approvers}.")
    if action == "review_existing_tool_first":
        return (f"Procurement to confirm with {requester} whether {', '.join(existing) or 'the existing approved tool'} already "
                f"meets the need before any purchase. If a real gap is confirmed, route to {approvers} for human approval.")
    if action == "route_for_reviews":
        return (f"Send this evidence pack to {approvers} for human review. The purchase must not proceed until every listed "
                "reviewer has signed off.")
    return f"Submit to {approvers} for human approval. The copilot has not approved anything."


def finalize(ctx: RunContext, draft: ModelDraft | None, architecture: str, usage: Usage, stages: list[dict],
             degraded_reason: str | None = None, model: str | None = None) -> AnalysisResult:
    a = ctx.assessment
    assert a is not None, "policy engine must run before finalize"
    events: list[GuardrailEvent] = []
    ledger_by_id = {e.evidence_id: e for e in ctx.ledger}
    request_text = json.dumps(ctx.request.model_dump(), default=str)
    flags = list(a.risk_flags)
    evidence: list[EvidenceItem] = []

    # ---- Evidence that is grounded by construction: tool summaries and fired policy rules
    for entry in ctx.ledger:
        if entry.tool in TOOLS and entry.tool != "evaluate_policy_rules" and entry.status not in ("invalid_arguments", "unknown_tool"):
            evidence.append(EvidenceItem(source=entry.tool, finding=entry.summary,
                                         reference=f"{entry.evidence_id} | {entry.reference}" if entry.reference else entry.evidence_id))
    for hit in a.rule_hits:
        evidence.append(EvidenceItem(source="evaluate_policy_rules", finding=hit.finding, reference=hit.policy_section))

    def grounded(text: str, cited: list[str], what: str) -> list[str] | None:
        """Return the valid cited ids if `text` is supported by them, else None (and record why)."""
        valid = [i for i in dict.fromkeys(cited) if i in ledger_by_id]
        if not text.strip() or not valid:
            events.append(GuardrailEvent(kind="ungrounded_finding", detail=f"{what} cited no valid evidence id: {text[:140]!r}"))
            return None
        allowed = _numeric_tokens(request_text + " " + " ".join(_entry_text(ledger_by_id[i]) for i in valid)) | _POLICY_CONSTANTS
        unsupported = sorted(_numeric_tokens(text) - allowed)
        if unsupported:
            events.append(GuardrailEvent(kind="unsupported_figure",
                                         detail=f"{what} states {unsupported} not found in {valid}: {text[:140]!r}"))
            return None
        if _APPROVAL_CLAIM.search(text):
            events.append(GuardrailEvent(kind="approval_language", detail=f"{what} claimed an approval: {text[:140]!r}"))
            return None
        return valid

    existing_products: list[str] = []
    proposed = None
    rationale = ""
    extra_questions: list[str] = []

    if draft is not None:
        proposed = draft.action
        catalog_ids = [e.evidence_id for e in ctx.ledger if e.tool == "search_software_catalog"]

        # ---- Overlap: the model's judgement counts only if it names products the catalog tool returned
        known = _catalog_products(ctx)
        existing_products = [known[name_key(p)] for p in draft.overlap_products if name_key(p) in known]
        if draft.overlap_level in _OVERLAP_FLAG_LEVELS:
            if not existing_products:
                events.append(GuardrailEvent(kind="ungrounded_overlap", detail=f"overlap '{draft.overlap_level}' named no product "
                                             f"returned by the catalog tool: {draft.overlap_products}"))
            else:
                if "existing_tool_overlap" not in flags:
                    flags.append("existing_tool_overlap")
                text = f"Overlap assessment ({draft.overlap_level.replace('_', ' ')}) with {', '.join(existing_products)}: {draft.overlap_reason}"
                if draft.overlap_reason and grounded(text, catalog_ids, "overlap assessment"):
                    evidence.append(EvidenceItem(source="agent_analysis", finding=text, reference=", ".join(catalog_ids)))

        # ---- Injection: accept the model's suspicion only if it quotes text that is really in the data
        if draft.injection_suspected and "prompt_injection_detected" not in flags:
            quote = " ".join((draft.injection_quote or "").split()).casefold().strip("\"'. ")
            if len(quote) >= 12 and quote[:80] in _business_text(ctx):
                flags.append("prompt_injection_detected")
                evidence.append(EvidenceItem(source="agent_analysis", reference="Policy section 9",
                                             finding=f"Instruction-like text in business data was ignored: \"{draft.injection_quote}\""))
            else:
                events.append(GuardrailEvent(kind="ungrounded_injection_claim",
                                             detail=f"model suspected injection but its quote is not in the data: {draft.injection_quote!r}"))

        # ---- Model-written findings must cite the ledger and match it
        for finding in draft.key_findings:
            valid = grounded(finding.finding, finding.evidence_ids, "finding")
            if valid:
                evidence.append(EvidenceItem(source="agent_analysis", finding=finding.finding.strip(), reference=", ".join(valid)))

        if draft.rationale and not _APPROVAL_CLAIM.search(draft.rationale) \
                and not (_numeric_tokens(draft.rationale) - _numeric_tokens(request_text + " " + " ".join(map(_entry_text, ctx.ledger))) - _POLICY_CONSTANTS):
            rationale = draft.rationale.strip()
        elif draft.rationale:
            events.append(GuardrailEvent(kind="rationale_replaced", detail=f"rationale was unsupported or claimed approval: {draft.rationale[:140]!r}"))
        extra_questions = [" ".join(q.split())[:240] for q in draft.clarification_questions if q.strip()][:2]

    # ---- Action: the model proposes, the policy results dispose
    overlap_supported = "existing_tool_overlap" in flags or bool(existing_products) or bool(a.overlap_candidates)
    if proposed in a.allowed_actions and (proposed != "review_existing_tool_first" or overlap_supported):
        action = proposed
    else:
        action = a.default_action
        if draft is not None:
            events.append(GuardrailEvent(kind="action_overridden",
                                         detail=f"model proposed '{proposed}', but policy results allow only {a.allowed_actions}; using '{action}'"))
            rationale = ""
    if not rationale:
        rationale = _fallback_rationale(action, a)

    # ---- Missing information: required fields from the engine; model questions only where they are actionable
    missing = list(a.missing_information)
    if action in ("request_clarification", "review_existing_tool_first"):
        missing += [q for q in extra_questions if q not in missing]

    if degraded_reason:
        flags.append("llm_unavailable")
        events.append(GuardrailEvent(kind="degraded_mode", detail=degraded_reason))
        evidence.append(EvidenceItem(source="system", reference="Policy section 10",
                                     finding="The AI model was unavailable, so this result contains deterministic checks only "
                                             "(no need analysis or overlap judgement). Treat as a manual-review package."))

    budget = next((e.output for e in ctx.ledger if e.tool == "check_budget"
                   and e.output.get("requester_id") == ctx.request.requester_id), {})
    requester = budget.get("requester_name") or ctx.request.requester_id or "the requester"
    if not existing_products:
        existing_products = [c.split(" (")[0] for c in a.overlap_candidates]

    ledger_tools = [e for e in ctx.ledger if e.tool in TOOLS and e.status not in ("invalid_arguments", "unknown_tool")]
    usage.tool_ms = round(sum(e.elapsed_ms for e in ctx.ledger), 1)
    decision = ProcurementDecision(
        request_id=ctx.request.request_id,
        recommendation=f"{ACTION_LABELS[action]}. {rationale}",
        evidence=evidence,
        required_approvals=list(a.required_approvals),
        missing_information=missing,
        risk_flags=flags,
        next_step=_next_step(action, a, requester, existing_products, missing),
        human_review_required=True,
        telemetry=RunTelemetry(llm_calls=usage.llm_calls, tool_calls=len(ledger_tools), tool_names=[e.tool for e in ledger_tools]),
    )
    return AnalysisResult(
        decision=decision, architecture=architecture, action=action, model_proposed_action=proposed,
        need_summary=draft.need_summary if draft else None, rationale=rationale,
        overlap_level=draft.overlap_level if draft else None, assessment=a, ledger=ctx.ledger,
        guardrail_events=events, stages=stages, usage=usage, degraded=bool(degraded_reason), model=model,
    )
