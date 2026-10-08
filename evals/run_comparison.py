"""Run both architectures over the same case set and score them the same way.

    python evals/run_comparison.py                       # all 18 cases, both architectures
    python evals/run_comparison.py --cases DS-06,FX-04   # a subset
    python evals/run_comparison.py --resume              # continue an interrupted run (e.g. after a quota limit)

Writes, under evals/results/<model>/:
    runs.jsonl    every run in full (decision, ledger, guardrail events, usage) - the audit trail
    results.csv   one row per run, in the shape of templates/evaluation_results_template.csv
    summary.md    the comparison table and per-case outcomes

Scoring is deterministic (no LLM judge). Per run:
    correct_next_action        final action is one the case accepts
    grounded_evidence          no model-written finding/claim had to be discarded by the grounding checks
    policy_followed            approvals match exactly, required flags present, forbidden flags absent, missing-info as expected
    human_escalation_correct   human review required, every expected specialist reviewer listed, not waved through
A case passes when all four hold. `model_action_correct` additionally records whether the model's own
proposal was right *before* the code guardrail, which is where the architectures can actually differ.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals._support import vendor_api  # noqa: E402
from evals.run_public_evals import evaluate as public_minimum_checks  # noqa: E402
from src.config import Settings  # noqa: E402
from src.llm import LLMError, LLMSettings  # noqa: E402
from src.models import AnalysisResult  # noqa: E402
from src.solution import analyze_request  # noqa: E402

FIXTURE_DIR = ROOT / "evals" / "fixtures" / "data"
FIXTURE_API = "http://127.0.0.1:8002"
SPECIALISTS = {"Security", "Privacy", "Legal", "Finance", "CFO"}
GROUNDING_EVENTS = {"ungrounded_finding", "unsupported_figure", "ungrounded_overlap", "ungrounded_injection_claim",
                    "approval_language", "rationale_replaced"}
CRITERIA = ["correct_next_action", "grounded_evidence", "policy_followed", "human_escalation_correct"]


def score(result: AnalysisResult, expected: dict) -> dict:
    d = result.decision
    notes: list[str] = []
    flags, approvals = set(d.risk_flags), set(d.required_approvals)

    correct_action = result.action in expected["actions"]
    if not correct_action:
        notes.append(f"action {result.action}, expected {expected['actions']}")

    rejected = [e for e in result.guardrail_events if e.kind in GROUNDING_EVENTS]
    notes += [f"{e.kind}: {e.detail[:110]}" for e in rejected]

    policy_problems = []
    if approvals != set(expected["approvals"]):
        extra, lacking = sorted(approvals - set(expected["approvals"])), sorted(set(expected["approvals"]) - approvals)
        policy_problems.append("approvals " + " ".join(([f"+{extra}"] if extra else []) + ([f"-{lacking}"] if lacking else [])))
    if missing_flags := sorted(set(expected["required_flags"]) - flags):
        policy_problems.append(f"flags missing {missing_flags}")
    if bad_flags := sorted(set(expected["forbidden_flags"]) & flags):
        policy_problems.append(f"unexpected flags {bad_flags}")
    text = " | ".join(d.missing_information).lower()
    for group in expected["missing_information_groups"]:
        if not any(token in text for token in group):
            policy_problems.append(f"missing-info lacks {group}")
    limit = expected["max_missing_information"]
    if limit is not None and len(d.missing_information) > limit:
        policy_problems.append(f"{len(d.missing_information)} missing-info items > {limit}")
    notes += policy_problems

    expected_specialists = set(expected["approvals"]) & SPECIALISTS
    waved_through = result.action == "proceed_to_standard_approval" and "proceed_to_standard_approval" not in expected["actions"]
    escalation = d.human_review_required and expected_specialists <= approvals and not waved_through
    if not escalation:
        notes.append("escalation: " + ("waved through" if waved_through else f"lacks {sorted(expected_specialists - approvals)}"))

    return {
        "correct_next_action": correct_action,
        "grounded_evidence": not rejected,
        "policy_followed": not policy_problems,
        "human_escalation_correct": bool(escalation),
        "model_action_correct": result.model_proposed_action in expected["actions"],
        "notes": "; ".join(notes),
    }


def run_one(case: dict, raw: dict, architecture: str, settings: Settings, trial: int, public: dict) -> dict:
    started = time.perf_counter()
    result = analyze_request(raw, architecture, settings)
    wall_ms = (time.perf_counter() - started) * 1000
    u, d = result.usage, result.decision
    row = {
        "case_id": case["case_id"], "architecture": architecture, "trial": trial, "request_id": case["request_id"],
        "model": result.model, "degraded": result.degraded, **score(result, case["expected"]),
        "action": result.action, "model_proposed_action": result.model_proposed_action,
        # processing time = model + tool time; waits for free-tier quota are reported separately, not hidden
        "latency_ms": round(u.llm_ms + u.tool_ms), "wall_ms": round(wall_ms), "rate_limit_wait_ms": round(u.rate_limit_wait_ms),
        "llm_calls": u.llm_calls, "llm_retries": u.llm_retries, "tool_calls": d.telemetry.tool_calls,
        "tokens": u.prompt_tokens + u.completion_tokens,
        "harness_backfilled_tools": sum(e.requested_by == "harness" for e in result.ledger),
        "model_findings_kept": sum(e.source == "agent_analysis" for e in d.evidence),
        "guardrail_events": [e.kind for e in result.guardrail_events],
    }
    row["passed"] = all(row[c] for c in CRITERIA) and not result.degraded
    if case["request_id"] in public:
        row["public_minimum_checks"] = not public_minimum_checks(d, public[case["request_id"]])
    return {**row, "result": result.model_dump(mode="json")}


# ---------------------------------------------------------------- reporting

CSV_COLUMNS = ["case_id", "architecture", "trial", *CRITERIA, "passed", "latency_ms", "llm_calls", "tool_calls", "notes",
               "action", "model_proposed_action", "model_action_correct", "tokens", "rate_limit_wait_ms", "wall_ms",
               "llm_retries", "harness_backfilled_tools", "model_findings_kept", "public_minimum_checks", "degraded", "model"]


def _avg(rows: list[dict], key: str) -> float:
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def write_reports(out_dir: Path, rows: list[dict], cases: list[dict], model: str) -> None:
    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["case_id"], r["architecture"], r["trial"])))

    scored = [r for r in rows if not r["degraded"]]
    archs = [a for a in ("single", "staged") if any(r["architecture"] == a for r in rows)]
    by_arch = {a: [r for r in scored if r["architecture"] == a] for a in archs}
    names = {"single": "A - single agent", "staged": "B - staged (analyst + reviewer)"}

    def frac(a: str, key: str) -> str:
        rs = by_arch[a]
        return f"{sum(bool(r[key]) for r in rs)}/{len(rs)}"

    metric_rows = [
        ("Cases passing all four criteria", lambda a: frac(a, "passed")),
        ("Correct next action (after guardrails)", lambda a: frac(a, "correct_next_action")),
        ("Model's own proposed action correct (before guardrails)", lambda a: frac(a, "model_action_correct")),
        ("Evidence grounded (no model claim discarded)", lambda a: frac(a, "grounded_evidence")),
        ("Policy + deterministic rules followed", lambda a: frac(a, "policy_followed")),
        ("Human escalation correct", lambda a: frac(a, "human_escalation_correct")),
        ("Public cases meeting the starter's minimum checks",
         lambda a: (lambda rs: f"{sum(r['public_minimum_checks'] for r in rs)}/{len(rs)}")([r for r in by_arch[a] if "public_minimum_checks" in r])),
        ("Avg processing latency (model + tools, ms)", lambda a: f"{_avg(by_arch[a], 'latency_ms'):,.0f}"),
        ("Avg LLM calls", lambda a: f"{_avg(by_arch[a], 'llm_calls'):.2f}"),
        ("Avg tool calls", lambda a: f"{_avg(by_arch[a], 'tool_calls'):.2f}"),
        ("Avg tokens per request", lambda a: f"{_avg(by_arch[a], 'tokens'):,.0f}"),
        ("Model findings kept / discarded by grounding checks",
         lambda a: f"{sum(r['model_findings_kept'] for r in by_arch[a])} / "
                   f"{sum(k in GROUNDING_EVENTS for r in by_arch[a] for k in r['guardrail_events'])}"),
        ("Model action overridden by policy guardrail (runs)", lambda a: str(sum("action_overridden" in r["guardrail_events"] for r in by_arch[a]))),
        ("Mandatory tools the agent skipped (run by harness)", lambda a: str(sum(r["harness_backfilled_tools"] for r in by_arch[a]))),
        ("Model call retries (invalid JSON / rate limit / 5xx)", lambda a: str(sum(r["llm_retries"] for r in by_arch[a]))),
    ]
    lines = [f"# Evaluation summary - {model}", "",
             f"{len(cases)} cases x {len(archs)} architectures, same cases, same tools, same policy engine, same scoring. "
             f"Runs scored: {len(scored)}; runs excluded because the model was unavailable: {len(rows) - len(scored)}.", "",
             "| Metric | " + " | ".join(names[a] for a in archs) + " |", "|---|" + "---:|" * len(archs)]
    lines += [f"| {label} | " + " | ".join(fn(a) for a in archs) + " |" for label, fn in metric_rows]

    lines += ["", "## Per case", "", "| Case | What it tests | " + " | ".join(f"{a}: action (pass?)" for a in archs) + " |",
              "|---|---|" + "---|" * len(archs)]
    for case in cases:
        cells = []
        for a in archs:
            rs = [r for r in rows if r["case_id"] == case["case_id"] and r["architecture"] == a]
            cells.append("; ".join(("model unavailable" if r["degraded"] else f"`{r['action']}` {'PASS' if r['passed'] else 'FAIL'}") for r in rs) or "-")
        lines.append(f"| {case['case_id']} {case['title']} | {case['edge_case']} | " + " | ".join(cells) + " |")

    problems = [r for r in scored if r["notes"]]
    lines += ["", "## Everything the scorer or the guardrails flagged", ""]
    lines += [f"- **{r['case_id']} / {r['architecture']}** ({'FAIL' if not r['passed'] else 'pass'}): {r['notes']}" for r in
              sorted(problems, key=lambda r: (r["case_id"], r["architecture"]))] or ["- nothing"]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--architectures", default="single,staged")
    parser.add_argument("--cases", default="all", help="'all', or comma-separated case ids (e.g. DS-01,FX-04)")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--resume", action="store_true", help="keep existing runs in the output folder and only run what is missing")
    parser.add_argument("--out", default=None, help="output folder (default: evals/results/<model>)")
    args = parser.parse_args()

    try:
        model = LLMSettings.from_env().model
    except LLMError as exc:
        sys.exit(f"Cannot evaluate without a model: {exc}")
    out_dir = Path(args.out) if args.out else ROOT / "evals" / "results" / re.sub(r"[^a-zA-Z0-9.-]+", "_", model)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_path = out_dir / "runs.jsonl"

    all_cases = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
    wanted = None if args.cases == "all" else set(args.cases.split(","))
    cases = [c for c in all_cases if wanted is None or c["case_id"] in wanted]
    architectures = args.architectures.split(",")
    public = {c["request_id"]: c["expectations"] for c in json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))}

    rows: list[dict] = []
    if args.resume and runs_path.is_file():
        rows = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = [r for r in rows if not r["degraded"]]       # a run without the model is not a result; redo it
    runs_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    done = {(r["case_id"], r["architecture"], r["trial"]) for r in rows}

    base = Settings.from_env()
    datasets = {
        "data": (base, {r["request_id"]: r for r in json.loads((base.data_dir / "requests.json").read_text(encoding="utf-8"))}),
        # a 1 s timeout keeps the simulated slow-API case quick; the service answers in milliseconds otherwise
        "fixtures": (Settings(data_dir=FIXTURE_DIR, vendor_risk_base_url=FIXTURE_API, vendor_risk_timeout_seconds=1.0),
                     {r["request_id"]: r for r in json.loads((FIXTURE_DIR / "requests.json").read_text(encoding="utf-8"))}),
    }
    print(f"Model: {model}\nCases: {len(cases)}  architectures: {architectures}  trials: {args.trials}  -> {out_dir.relative_to(ROOT)}\n")

    stopped = None
    with vendor_api(base.vendor_risk_base_url) as a, vendor_api(FIXTURE_API, FIXTURE_DIR / "vendor_risk.json") as b:
        print(f"Vendor-risk API (data): {a}; (fixtures): {b}\n")
        for trial in range(1, args.trials + 1):
            for case in cases:
                settings, requests_by_id = datasets[case["dataset"]]
                for architecture in architectures:       # A then B on the same case, back to back
                    if (case["case_id"], architecture, trial) in done:
                        continue
                    row = run_one(case, requests_by_id[case["request_id"]], architecture, settings, trial, public)
                    if row["degraded"]:
                        reason = next((e["detail"] for e in row["result"]["guardrail_events"] if e["kind"] == "degraded_mode"), "")
                        print(f"STOP  {case['case_id']} [{architecture}] model unavailable: {reason}")
                        stopped = reason
                        break
                    rows.append(row)
                    with runs_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row) + "\n")
                    print(f"{'PASS' if row['passed'] else 'FAIL'}  {case['case_id']} [{architecture:6}] {row['action']:28} "
                          f"{row['latency_ms']:>6} ms  {row['llm_calls']} llm  {row['tool_calls']} tools  {row['tokens']:>5} tok"
                          + (f"  (waited {row['rate_limit_wait_ms'] / 1000:.0f}s for quota)" if row["rate_limit_wait_ms"] else "")
                          + (f"\n      {row['notes']}" if row["notes"] else ""))
                if stopped:
                    break
            if stopped:
                break

    write_reports(out_dir, rows, cases, model)
    print(f"\nWrote {out_dir.relative_to(ROOT)}/summary.md, results.csv, runs.jsonl")
    if stopped:
        sys.exit("Stopped early because the model became unavailable (likely a quota limit). "
                 "Re-run with --resume to finish; completed runs are kept.")


if __name__ == "__main__":
    main()
