# Architecture Decision Memo

## Decision

Ship **Architecture A, the single agent**.

## Evidence

Same 18 cases, tools, policy engine and scoring for both; one trial per case; two models.

| Metric | A, 120b | B, 120b | A, 20b | B, 20b |
|---|---:|---:|---:|---:|
| Cases passing all four criteria | 16/18 | 16/18 | 16/18 | 16/18 |
| Correct next action | 17/18 | 16/18 | 16/18 | 16/18 |
| Evidence grounded | 18/18 | 18/18 | 18/18 | 18/18 |
| Policy rules followed | 16/18 | 17/18 | 17/18 | 18/18 |
| Human escalation correct | 18/18 | 17/18 | 17/18 | 18/18 |
| Avg processing latency | 3.5 s | 5.0 s | 2.4 s | 3.7 s |
| Avg LLM calls | 2.00 | 3.06 | 2.06 | 3.06 |
| Avg tool calls | 5.00 | 5.06 | 5.06 | 5.06 |
| Avg tokens per request | 3,547 | 5,450 | 3,519 | 5,184 |

Notable failures: all eight, across both architectures and models, are in the one judgement left to the model: whether an existing tool already covers the need. Each architecture waved one duplicate through once (B on 120b, A on 20b). No approval, threshold, expiry, outage or injection case went wrong; those are decided in shared code.

## Trade-offs

B did not improve quality: it passed the same number of cases on both models.

B cost more on every request: one extra model call, about 50% more tokens and 45-55% more processing time. Under an 8,000 tokens-per-minute limit that means longer queueing (26-31 s average wait against 18-19 s).

B was also less stable on the smaller model: seven retried model calls against none for A, and its model's own proposal was right in 13 of 18 runs against 15 for A before the code guardrail corrected it.

B has one real advantage. Its reviewer never sees requester free text or vendor notes, which narrows the injection surface. That advantage did not show up in results: all three injection cases came out right in both architectures, and approvals are outside the model's control in both.

## Risks / limitations

- 18 cases and one trial each cannot separate two systems that both score 16/18; the cost difference is the only consistent signal.
- I wrote both the expected outcomes and the policy engine.
- The 120b figures were rebuilt from the console log after the detailed record was lost; the 20b record is complete and replayable.
- Before production I would validate overlap judgement on a larger, independently labelled set with repeated trials.

## Why this is the right MVP

Reliability in this product comes from the parts both architectures share: deterministic policy rules, mandatory evidence checks run regardless of what the agent requests, grounding checks on every model-written finding, and a human decision at the end. A second agent cannot improve what the model does not control. It can only re-judge the action, and here it did that no better while costing half as much again. The single agent is easier to trace, test and operate, and leaves headroom on the rate limit. I would revisit B only if a larger evaluation showed the reviewer catching overlap errors that the single agent makes.
