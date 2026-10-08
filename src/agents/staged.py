"""Architecture B - staged two-agent variant.

Procurement Analyst: plans and runs the tools, then writes a structured evidence
pack. It does not recommend an action.
Policy/Risk Reviewer: has no tools. It receives the evidence pack plus the tool
results, challenges findings the tool results do not support, and chooses the
next action.

The handoff is deliberate about untrusted text: the reviewer sees the request's
structured fields, deterministic one-line summaries of each lookup and the
policy engine's output, but not the requester's free-text justification or
vendor notes. Those reach it only second-hand, through the analyst's pack.
"""
from __future__ import annotations

import json

from src.agents import common
from src.agents.common import ModelDraft
from src.llm import JsonLLM
from src.models import ACTION_LABELS, OVERLAP_LEVELS, Usage
from src.tools import RunContext

ANALYST_SYSTEM = (
    "You are the Procurement Analyst in a two-stage procurement review. You gather evidence and summarise what it shows. "
    "You do NOT recommend an action; a separate Policy/Risk Reviewer does that from your evidence pack.\n"
    + common.CORE_RULES
)

ANALYST_PACK_INSTRUCTIONS = f"""\
The evidence you asked for is below. Write the evidence pack for the Policy/Risk Reviewer.

{common.OVERLAP_GUIDE}

{common.FINDINGS_GUIDE}
open_questions: at most 2 things the requester would need to answer; otherwise [].
{common.FOLLOW_UP_GUIDE}"""

REVIEWER_SYSTEM = (
    "You are the Policy/Risk Reviewer in a two-stage procurement review. You have no tools. You receive the request's "
    "structured fields, the tool results, and an evidence pack written by a Procurement Analyst. Independently check "
    "the analyst's work against the tool results, then choose the next action.\n"
    + common.CORE_RULES
)

REVIEWER_INSTRUCTIONS = f"""\
Review the analyst's evidence pack against the tool results and decide.

{common.ACTION_GUIDE}

rejected_finding_numbers: the numbers of any analyst findings that the cited tool results do not support (wrong figure, wrong status, or a claim not in the evidence); otherwise [].
overlap: your own judgement, using the analyst's pack as input.
{common.OVERLAP_GUIDE}
rationale: at most 40 words, facts only, explaining the action.
clarification_questions: at most 2, only for something the requester must answer before review can continue; otherwise []."""


def run(ctx: RunContext, llm: JsonLLM, usage: Usage, stages: list[dict]) -> ModelDraft:
    draft = ModelDraft()

    # ---- Stage 1: Procurement Analyst
    request_block = common.render_request(ctx.request)
    plan = llm.complete_json(ANALYST_SYSTEM, f"{common.PLAN_INSTRUCTIONS}\n\n{request_block}", "evidence_plan",
                             common.plan_schema(), usage)
    draft.need_summary = str(plan.get("need_summary") or "")[:400]
    common.execute_tool_requests(ctx, plan.get("tool_requests"))
    common.ensure_policy_evaluated(ctx)
    stages.append({"stage": "plan", "agent": "Procurement Analyst", "output": plan})

    for round_number in range(common.MAX_FOLLOW_UP_ROUNDS + 1):
        final_round = round_number == common.MAX_FOLLOW_UP_ROUNDS
        user = f"{ANALYST_PACK_INSTRUCTIONS}\n\n{request_block}\n{common.render_ledger(ctx.ledger)}"
        if final_round:
            user += "\nNo further searches are available: set follow_up_catalog_keywords to []."
        pack = llm.complete_json(ANALYST_SYSTEM, user, "evidence_pack", common.evidence_pack_schema(), usage)
        stages.append({"stage": "evidence_pack", "agent": "Procurement Analyst", "output": pack})
        if final_round or not common.run_follow_up_search(ctx, pack.get("follow_up_catalog_keywords")):
            break
    draft.apply_evidence_fields(pack)

    # ---- Handoff: structured evidence pack + tool results (no requester free text)
    handoff = {
        "need_summary": draft.need_summary,
        "overlap": {"assessment": draft.overlap_level, "existing_products": draft.overlap_products, "reason": draft.overlap_reason},
        "injection_suspected": draft.injection_suspected,
        "findings": [{"number": i, "finding": f.finding, "evidence_ids": f.evidence_ids}
                     for i, f in enumerate(draft.key_findings, start=1)],
        "open_questions": [str(q) for q in (pack.get("open_questions") or [])][:2],
    }
    reviewer_input = (
        f"{REVIEWER_INSTRUCTIONS}\n\n{common.render_request(ctx.request, include_free_text=False)}\n"
        f"{common.render_ledger(ctx.ledger, summaries_only=True)}\n<analyst_pack>\n{json.dumps(handoff, separators=(',', ':'), ensure_ascii=False)}\n</analyst_pack>"
    )

    # ---- Stage 2: Policy/Risk Reviewer
    review = llm.complete_json(REVIEWER_SYSTEM, reviewer_input, "policy_risk_review", common.review_schema(), usage)
    stages.append({"stage": "review", "agent": "Policy/Risk Reviewer", "output": review, "handoff": handoff})

    rejected = {n for n in (review.get("rejected_finding_numbers") or []) if isinstance(n, int)}
    draft.key_findings = [f for i, f in enumerate(draft.key_findings, start=1) if i not in rejected]
    if isinstance(review.get("overlap"), dict) and review["overlap"].get("assessment") in OVERLAP_LEVELS:
        analyst_products = draft.overlap_products
        draft.apply_overlap(review["overlap"])
        draft.overlap_products = draft.overlap_products or analyst_products
    draft.action = review.get("action") if review.get("action") in ACTION_LABELS else None
    draft.rationale = str(review.get("rationale") or "")[:500]
    draft.clarification_questions = [str(q) for q in (review.get("clarification_questions") or []) if q]
    return draft
