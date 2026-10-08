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
MODEL_NAME=gemini-3.5-flash-lite python evals/run_comparison.py --replay   # reproduce a committed table; no key, no quota
MODEL_NAME=openai/gpt-oss-20b python evals/run_comparison.py --replay

python evals/run_comparison.py --out evals/results/my-run   # new live run: all 18 cases, both architectures
python evals/run_comparison.py --cases DS-06,FX-04 --out evals/results/scratch
python evals/run_comparison.py --trials 3 --out evals/results/three-trials
python evals/run_comparison.py --resume               # continue after a quota stop
python evals/run_comparison.py --overwrite            # discard the recorded runs for this model and start again
python evals/run_comparison.py --report-only          # rebuild reports from saved runs (no model calls)
```

Recorded runs cost model quota to recreate, so a live run refuses to write over them unless you pass `--resume`, `--overwrite` or a different `--out`.

`--replay` takes the model outputs recorded in a live run and feeds them back through the real tools, policy engine and finalizer. It needs no model, so anyone can reproduce the published numbers, and it shows what a code change does to past model behaviour. The first replay preserves the live record as `runs_live.jsonl`; the summary lists every outcome that differs from it.

Output goes to `evals/results/<model>/`:

| File | Contents |
|---|---|
| `summary.md` | comparison table, per-case outcomes, every failure and guardrail event, provenance |
| `results.csv` | one row per run, in the starter template's columns plus cost and trace counters |
| `runs.jsonl` | the full decision, ledger, agent stages and guardrail events for every run, as scored by the current code |
| `runs_live.jsonl` | the same, exactly as the live run produced it; the source for `--replay` |
| `live_console.log` | what the live run printed |

**Committed results.** `gemini-3.5-flash-lite/` is one uninterrupted live run on the final code, so its `runs.jsonl` is the live record. `openai_gpt-oss-20b/` has all of the above. `openai_gpt-oss-120b/` has only `summary.md`, `results.csv` and `live_console.log`: its detailed record was lost during development, and `reconstruct_from_console.py` rebuilt the report from the console log, marking the counters the console does not print as "not recorded".

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

- 18 cases and one trial each: enough to find failures, not enough to prove a difference between architectures (the totals are 49/54 and 50/54).
- Expected outcomes and the policy engine have the same author.
- Approvals and policy flags come from shared code, so the two architectures can only differ on the action, model-written findings, model-inferred data classes and cost. That is a property of the design, and it is the main reason the comparison comes out the way it does.
