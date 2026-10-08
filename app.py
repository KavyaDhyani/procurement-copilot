"""Procurement Request Copilot - reviewer UI.

Three panels, as the brief asks: request details, evidence, recommendation + action.
The copilot recommends; the "Human decision" form is where a person decides.
"""
from __future__ import annotations

import time

import pandas as pd
import requests
import streamlit as st

from src import data_access
from src.audit import HUMAN_DECISIONS, load_human_decisions, record_human_decision
from src.config import Settings
from src.llm import LLMError, LLMSettings
from src.models import ACTION_LABELS, AnalysisResult
from src.solution import analyze_request

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")

SETTINGS = Settings.from_env()
ARCH_LABELS = {"single": "A - single agent", "staged": "B - analyst + reviewer"}
ACTION_STYLE = {
    "request_clarification": st.warning, "manual_review": st.error, "review_existing_tool_first": st.warning,
    "route_for_reviews": st.info, "proceed_to_standard_approval": st.success,
}


# ---------------------------------------------------------------- helpers

def model_status() -> tuple[bool, str]:
    try:
        s = LLMSettings.from_env()
        return True, f"{s.provider} / {s.model}"
    except LLMError as exc:
        return False, str(exc)


def api_status() -> bool:
    try:
        return requests.get(SETTINGS.vendor_risk_base_url + "/health", timeout=0.5).ok
    except requests.RequestException:
        return False


def run(raw: dict, architecture: str) -> AnalysisResult:
    started = time.perf_counter()
    result = analyze_request(raw, architecture, SETTINGS)
    st.session_state.setdefault("wall_ms", {})[(raw["request_id"], architecture)] = (time.perf_counter() - started) * 1000
    return result


def chips(items: list[str], empty: str) -> str:
    return " ".join(f"`{i}`" for i in items) if items else f"_{empty}_"


# ---------------------------------------------------------------- panels

def request_panel(raw: dict) -> None:
    st.subheader("1 · Request details")
    employee = data_access.find_employee(raw.get("requester_id"), SETTINGS.data_dir)
    who = f"{employee['name']} · {employee['department']}" if employee else f"{raw.get('requester_id')} (not in directory)"
    cost, users = raw.get("annual_cost_usd"), raw.get("user_count")
    a, b, c = st.columns(3)
    a.metric("Annual cost", f"${cost:,.0f}" if isinstance(cost, (int, float)) else "not given")
    b.metric("Users", users if users is not None else "not given")
    c.metric("Urgency", raw.get("urgency") or "-")
    st.markdown(
        f"**{raw.get('product_name') or '(no product)'}** from **{raw.get('vendor_name') or '(no vendor)'}** "
        f"· {raw.get('category') or 'uncategorised'}  \n"
        f"Requested by {who} · `{raw['request_id']}`  \n"
        f"Data access: `{raw.get('data_access_level') or 'not given'}` · Integrations: "
        f"{chips(raw.get('requested_integrations') or [], 'none')}"
    )
    st.caption("Requester's justification - untrusted text; shown as written, never treated as an instruction")
    st.code(raw.get("business_justification") or "(none provided)", language=None, wrap_lines=True)


def recommendation_panel(result: AnalysisResult) -> None:
    d = result.decision
    st.subheader("3 · Recommendation and next action")
    label, _, rationale = d.recommendation.partition(". ")
    ACTION_STYLE[result.action](f"**{label}**\n\n{rationale}")
    if result.degraded:
        st.error("The AI model was unavailable. This is a deterministic-only result: rules and evidence are complete, "
                 "but there is no need analysis or overlap judgement.")
    st.markdown(f"**Next step:** {d.next_step}")
    st.markdown(f"**Approvals required (human):** {chips(d.required_approvals, 'cannot be determined until the request is complete')}")
    st.markdown(f"**Risk flags:** {chips(d.risk_flags, 'none')}")
    if d.missing_information:
        st.markdown("**Missing information:**\n" + "\n".join(f"- {m}" for m in d.missing_information))
    else:
        st.markdown("**Missing information:** _none_")
    if result.need_summary:
        st.caption(f"Need as understood by the copilot: {result.need_summary}")
    st.caption("Advisory only. The copilot cannot approve, purchase or change budgets; a human must decide below.")


def evidence_panel(result: AnalysisResult) -> None:
    st.subheader("2 · Evidence")
    d = result.decision
    groups = {
        "Tool results": [e for e in d.evidence if e.source not in ("evaluate_policy_rules", "agent_analysis", "system")],
        "Policy rules applied (deterministic)": [e for e in d.evidence if e.source == "evaluate_policy_rules"],
        "AI analysis (each finding checked against the tool results it cites)": [e for e in d.evidence if e.source in ("agent_analysis", "system")],
    }
    for title, items in groups.items():
        st.markdown(f"**{title}**")
        if not items:
            st.caption("none")
            continue
        st.dataframe(pd.DataFrame([{"Source": e.source, "Finding": e.finding, "Reference": e.reference} for e in items]),
                     hide_index=True, width="stretch",
                     column_config={"Finding": st.column_config.TextColumn(width="large")})


def trace_panel(result: AnalysisResult, wall_ms: float | None) -> None:
    u, d = result.usage, result.decision
    with st.expander("How this was produced - agents, tool calls, guardrails, cost"):
        cols = st.columns(6)
        cols[0].metric("Model calls", u.llm_calls)
        cols[1].metric("Tool calls", d.telemetry.tool_calls)
        cols[2].metric("Tokens", f"{u.prompt_tokens + u.completion_tokens:,}")
        cols[3].metric("Model time", f"{u.llm_ms / 1000:.1f}s")
        cols[4].metric("Quota wait", f"{u.rate_limit_wait_ms / 1000:.1f}s")
        cols[5].metric("Wall clock", f"{wall_ms / 1000:.1f}s" if wall_ms else "-")
        st.caption(f"Architecture: {ARCH_LABELS[result.architecture]} · model: {result.model or 'none'} · "
                   f"model's proposed action: `{result.model_proposed_action}` → final action: `{result.action}`")

        st.markdown("**Guardrail events** (where code overruled or discarded model output)")
        if result.guardrail_events:
            for event in result.guardrail_events:
                st.markdown(f"- `{event.kind}` - {event.detail}")
        else:
            st.caption("none - the model's output was consistent with the policy results and the evidence")

        st.markdown("**Evidence ledger** (every tool execution; `harness` = a mandatory check the agent did not request)")
        st.dataframe(pd.DataFrame([{"ID": e.evidence_id, "Tool": e.tool, "Requested by": e.requested_by, "Status": e.status,
                                    "Arguments": str(e.arguments), "ms": e.elapsed_ms} for e in result.ledger]),
                     hide_index=True, width="stretch")
        st.markdown("**Agent stages** (raw structured output of each model call)")
        for stage in result.stages:
            st.markdown(f"_{stage['agent']} - {stage['stage']}_")
            st.json(stage["output"], expanded=False)


def human_review_panel(result: AnalysisResult) -> None:
    request_id = result.decision.request_id
    st.subheader("4 · Human decision")
    with st.form(f"human-{request_id}-{result.architecture}"):
        a, b = st.columns([1, 2])
        reviewer = a.text_input("Your name")
        choice = b.selectbox("Decision", HUMAN_DECISIONS)
        note = st.text_area("Note / reason (required when you depart from the recommendation)", height=80)
        if st.form_submit_button("Record decision"):
            if not reviewer.strip():
                st.error("Enter your name: decisions are attributed to a person.")
            else:
                record_human_decision(result, reviewer, choice, note)
                st.success("Recorded. Nothing has been purchased or approved by the copilot.")
    history = load_human_decisions(request_id)
    if history:
        st.dataframe(pd.DataFrame([{"When (UTC)": h["recorded_at"], "Reviewer": h["reviewer"], "Decision": h["human_decision"],
                                    "Copilot recommended": ACTION_LABELS.get(h["copilot"]["action"], h["copilot"]["action"]),
                                    "Note": h["note"]} for h in history]), hide_index=True, width="stretch")


def comparison_panel(results: dict[str, AnalysisResult]) -> None:
    st.subheader("Architecture comparison for this request")
    rows = {}
    for arch, r in results.items():
        u = r.usage
        rows[ARCH_LABELS[arch]] = {
            "Final action": r.action, "Model proposed": r.model_proposed_action or "-",
            "Approvals": ", ".join(r.decision.required_approvals) or "-",
            "Risk flags": ", ".join(r.decision.risk_flags) or "-",
            "AI findings kept": sum(e.source == "agent_analysis" for e in r.decision.evidence),
            "Guardrail events": len(r.guardrail_events), "Model calls": u.llm_calls,
            "Tool calls": r.decision.telemetry.tool_calls, "Tokens": u.prompt_tokens + u.completion_tokens,
            "Model time (s)": round(u.llm_ms / 1000, 1),
        }
    st.dataframe(pd.DataFrame(rows).astype(str), width="stretch")
    st.caption("Approvals and policy flags come from the same deterministic engine in both architectures, so they should "
               "match. Differences show up in the action, the AI findings, and the cost.")


def new_request_form() -> dict | None:
    employees = data_access.load_employees(SETTINGS.data_dir)
    with st.sidebar.form("new-request"):
        requester = st.selectbox("Requester", [e["employee_id"] for e in employees],
                                 format_func=lambda i: next(f"{e['name']} ({e['department']})" for e in employees if e["employee_id"] == i))
        product = st.text_input("Product")
        vendor = st.text_input("Vendor")
        category = st.text_input("Category")
        cost = st.text_input("Annual cost (USD) - leave blank if unknown")
        users = st.text_input("Users - leave blank if unknown")
        data_level = st.text_input("Data-access level", placeholder="e.g. none, customer_pii, source_code")
        integrations = st.text_input("Integrations (comma separated)")
        justification = st.text_area("Business justification")
        if not st.form_submit_button("Use this request"):
            return st.session_state.get("adhoc")
    st.session_state["adhoc"] = {
        "request_id": f"REQ-ADHOC-{int(time.time()) % 100000}", "requester_id": requester, "product_name": product or None,
        "vendor_name": vendor or None, "category": category or None, "annual_cost_usd": cost or None,
        "user_count": users or None, "business_justification": justification or None,
        "data_access_level": data_level or None,
        "requested_integrations": [i.strip() for i in integrations.split(",") if i.strip()], "urgency": "normal",
    }
    return st.session_state["adhoc"]


# ---------------------------------------------------------------- page

st.title("Procurement Request Copilot")
st.caption("Gathers evidence, applies procurement policy in code, and recommends a next action. Humans approve.")

model_ok, model_text = model_status()
with st.sidebar:
    st.markdown("### Request")
    source = st.radio("Source", ["Sample request", "New request"], horizontal=True, label_visibility="collapsed")
if source == "Sample request":
    samples = {r["request_id"]: r for r in data_access.load_requests(SETTINGS.data_dir)}
    request_id = st.sidebar.selectbox("Request", list(samples), format_func=lambda i: f"{i} - {samples[i]['product_name']}")
    raw_request = samples[request_id]
else:
    raw_request = new_request_form()

with st.sidebar:
    st.markdown("### Architecture")
    mode = st.radio("Architecture", ["single", "staged", "compare"], label_visibility="collapsed",
                    format_func=lambda m: {**ARCH_LABELS, "compare": "Compare A and B"}[m])
    go = st.button("Run analysis", type="primary", width="stretch", disabled=raw_request is None)
    st.markdown("### Status")
    st.markdown(("🟢" if model_ok else "🔴") + f" Model: {model_text if model_ok else 'not configured'}")
    st.markdown(("🟢" if api_status() else "🔴") + f" Vendor-risk API: {SETTINGS.vendor_risk_base_url}")
    if not model_ok:
        st.caption("Without a model key the copilot still runs every deterministic check and says so.")
    st.caption("Free model tiers allow ~8,000 tokens/minute; a run uses 3-5k, so back-to-back runs may pause briefly.")

if raw_request is None:
    st.info("Fill in the new-request form in the sidebar and choose **Use this request**.")
    st.stop()

architectures = ["single", "staged"] if mode == "compare" else [mode]
results: dict = st.session_state.setdefault("results", {})
if go:
    for arch in architectures:
        with st.spinner(f"Running {ARCH_LABELS[arch]} ..."):
            results[(raw_request["request_id"], arch)] = run(raw_request, arch)

shown = {arch: results[(raw_request["request_id"], arch)] for arch in architectures if (raw_request["request_id"], arch) in results}

left, right = st.columns([0.9, 1.1], gap="large")
with left:
    request_panel(raw_request)
if not shown:
    with right:
        st.subheader("3 · Recommendation and next action")
        st.info("Choose **Run analysis** to gather evidence and get a recommendation.")
    st.stop()

if mode == "compare" and len(shown) == 2:
    with right:
        comparison_panel(shown)
    tabs = st.tabs([ARCH_LABELS[a] for a in shown])
    for tab, (arch, result) in zip(tabs, shown.items()):
        with tab:
            recommendation_panel(result)
            evidence_panel(result)
            trace_panel(result, st.session_state.get("wall_ms", {}).get((raw_request["request_id"], arch)))
else:
    arch, result = next(iter(shown.items()))
    with right:
        recommendation_panel(result)
    evidence_panel(result)
    trace_panel(result, st.session_state.get("wall_ms", {}).get((raw_request["request_id"], arch)))
    human_review_panel(result)
