# Evaluation

Two scripts, both calling the same `handle_request` / `analyze_request` code path. Both start the mock vendor-risk API themselves if it is not already running.

## 1. Public minimum checks (starter harness)

```bash
python evals/run_public_evals.py --architecture single
python evals/run_public_evals.py --architecture staged
```

Runs the six public cases in `public_cases.json`, validates the output schema, applies the starter's minimum expectations and writes `evals/results_<architecture>.csv` (git-ignored scratch output). Its latency column is wall-clock time, which on a free model tier includes waiting for per-minute quota.

## 2. A-vs-B comparison

```bash
python evals/run_comparison.py                        # all 18 cases, both architectures
python evals/run_comparison.py --cases DS-06,FX-04    # a subset
python evals/run_comparison.py --trials 3             # repeat each case
python evals/run_comparison.py --resume               # continue after a quota stop
python evals/run_comparison.py --report-only          # rebuild reports from saved runs (no model calls)
MODEL_NAME=openai/gpt-oss-20b python evals/run_comparison.py
```

Output goes to `evals/results/<model>/`:

| File | Contents |
|---|---|
| `summary.md` | comparison table, per-case outcomes, every failure and guardrail event |
| `results.csv` | one row per run, in the starter template's columns plus cost and trace counters |
| `runs.jsonl` | the full decision, ledger, agent stages and guardrail events for every run |

### Cases (`cases.json`)

| Set | Cases | Data |
|---|---|---|
| `DS-01` to `DS-10` | every request in `data/requests.json`; six of them are the public cases | `data/` |
| `FX-01` to `FX-08` | hidden-case proxies: different vendors, values and wording | `evals/fixtures/data/`, served by a second mock API on port 8002 |

Each case lists the accepted action(s), the exact approvals, flags that must and must not appear, and missing-information expectations. They were written by hand from `data/procurement_policy.md`.

### Scoring

Deterministic; no LLM judge.

| Column | True when |
|---|---|
| `correct_next_action` | the final action is one the case accepts |
| `grounded_evidence` | no model-written finding, overlap claim, injection claim or rationale was discarded by the grounding checks |
| `policy_followed` | approvals match exactly, required flags present, forbidden flags absent, missing information as expected |
| `human_escalation_correct` | human review required, every expected specialist reviewer present, not waved through |
| `passed` | all four, and the model was available |
| `model_action_correct` | the model's own proposal, before the code guardrail, was an accepted action |

`latency_ms` is model time plus tool time. `rate_limit_wait_ms` and `wall_ms` are reported next to it so quota waits are visible rather than mixed in.

### Limits of this evaluation

- 18 cases and one trial each: enough to find failures, not enough to prove a difference between architectures.
- Expected outcomes and the policy engine have the same author.
- Approvals and policy flags come from shared code, so the two architectures can only differ on the action, model-written findings, model-inferred data classes and cost. That is a property of the design, and it is the main reason the comparison comes out the way it does.
