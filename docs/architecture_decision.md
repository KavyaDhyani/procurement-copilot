# Architecture Decision Memo

## Decision

Ship **Architecture A, the single agent**.

## Evidence

Same 18 cases, tools, policy engine and scoring for both; one trial per case; three models (Gemini 3.5 Flash-Lite, gpt-oss-120b, gpt-oss-20b).

| Metric | A, Lite | B, Lite | A, 120b | B, 120b | A, 20b | B, 20b |
|---|---:|---:|---:|---:|---:|---:|
| Cases passing all criteria | 17/18 | 18/18 | 16/18 | 16/18 | 16/18 | 16/18 |
| Avg latency | 4.2 s | 6.6 s | 3.5 s | 5.0 s | 2.4 s | 3.7 s |
| Avg LLM calls | 2.00 | 3.00 | 2.00 | 3.06 | 2.06 | 3.06 |
| Avg tool calls | 5.00 | 5.00 | 5.00 | 5.06 | 5.06 | 5.06 |
| Avg tokens | 2,845 | 4,331 | 3,547 | 5,450 | 3,519 | 5,184 |

Totals: A 49/54, B 50/54.

Notable failures: all nine are the one judgement left to the model, whether an existing tool already covers the need. A sent a duplicate request to standard approval twice (Lite, 20b), B once (120b). No approval, threshold, expiry, outage or injection case went wrong; shared code decides those.

## Trade-offs

B's quality edge is one run in 54, on one model, and it reverses on another. Its cost is the same on all three: one extra model call, about 50% more tokens and 45-55% more latency.

B did get the duplicate-tool case right twice where A did not. That failure is contained: the request still goes to human approvers, and the evidence panel shows the existing product and the rule asking them to confirm it is not a duplicate. What is lost is a flag and a more cautious action, not human review.

B's reviewer sees no requester free text or vendor notes, a narrower injection surface. It made no difference: every injection case came out right in both.

## Risks / limitations

- One trial per case cannot separate 49/54 from 50/54. More trials could show B ahead on overlap judgement.
- I wrote both the expected outcomes and the policy engine.
- The 120b figures were rebuilt from a console log; the other two records are complete and replayable.
- Before production: a larger, independently labelled overlap set with repeated trials, and a test of defaulting ambiguous same-vendor overlap to "check the existing tool" in code, which targets the same failure without a second agent.

## Why this is the right MVP

Reliability here comes from what both architectures share: deterministic policy rules, mandatory evidence checks, grounding checks on model-written findings, and a human decision. A second agent cannot improve what the model does not control. It can only re-judge the action; there it was right one more time in 54 while costing half as much again on every request. A is easier to trace, test and operate. I would switch to B if repeated trials showed it reliably catching duplicates that A misses and a code-level default did not close the gap.
