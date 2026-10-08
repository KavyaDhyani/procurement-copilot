"""Architecture A - single-agent baseline.

One agent, one system prompt, one conversation of work: it plans which tools to
run, reads the results, and recommends the next action. Typically two model
calls (plan, decide); a third only if it asks for follow-up evidence.
"""
from __future__ import annotations

from src.agents import common
from src.agents.common import ModelDraft
from src.llm import JsonLLM
from src.models import ACTION_LABELS, Usage
from src.tools import RunContext

SYSTEM = "You are the Procurement Request Copilot for an internal procurement team. " + common.CORE_RULES

DECIDE_INSTRUCTIONS = f"""\
The evidence you asked for is below. Recommend the next action.

{common.ACTION_GUIDE}

{common.OVERLAP_GUIDE}

{common.FINDINGS_GUIDE}
rationale: at most 40 words, facts only, explaining the action.
clarification_questions: at most 2, only for something the requester must answer before review can continue; otherwise [].
{common.FOLLOW_UP_GUIDE}"""


def run(ctx: RunContext, llm: JsonLLM, usage: Usage, stages: list[dict]) -> ModelDraft:
    request_block = common.render_request(ctx.request)
    draft = ModelDraft()

    plan = llm.complete_json(SYSTEM, f"{common.PLAN_INSTRUCTIONS}\n\n{request_block}", "evidence_plan", common.plan_schema(), usage)
    draft.need_summary = str(plan.get("need_summary") or "")[:400]
    common.execute_tool_requests(ctx, plan.get("tool_requests"))
    common.ensure_policy_evaluated(ctx)
    stages.append({"stage": "plan", "agent": "Procurement Agent", "output": plan})

    for round_number in range(common.MAX_FOLLOW_UP_ROUNDS + 1):
        final_round = round_number == common.MAX_FOLLOW_UP_ROUNDS
        user = f"{DECIDE_INSTRUCTIONS}\n\n{request_block}\n{common.render_ledger(ctx.ledger)}"
        if final_round:
            user += "\nNo further searches are available: set follow_up_catalog_keywords to []."
        decision = llm.complete_json(SYSTEM, user, "recommendation", common.decision_schema(), usage)
        stages.append({"stage": "decide", "agent": "Procurement Agent", "output": decision})
        if final_round or not common.run_follow_up_search(ctx, decision.get("follow_up_catalog_keywords")):
            break

    draft.apply_evidence_fields(decision)
    draft.action = decision.get("action") if decision.get("action") in ACTION_LABELS else None
    draft.rationale = str(decision.get("rationale") or "")[:500]
    draft.clarification_questions = [str(q) for q in (decision.get("clarification_questions") or []) if q]
    return draft
