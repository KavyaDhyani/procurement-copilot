# Submission checklist

Each item names where it is satisfied.

- [x] `python verify_setup.py` passes in the project environment.
- [x] The product can process a request end-to-end - `python run_local.py`, then **Run analysis** in the UI.
- [x] Architecture A is a working single-agent baseline - `src/agents/single.py`.
- [x] Architecture B is a lightweight staged / 2-agent variant - `src/agents/staged.py`.
- [x] At least 3 tools are used; at least 1 is deterministic - 5 tools in `src/tools.py`, 4 deterministic.
- [x] Recommendations are returned in the `ProcurementDecision` structure - `src/finalizer.py`; contract unchanged.
- [x] Important evidence is visible to the user - evidence panel, grouped by tool results / policy rules / AI analysis.
- [x] Missing, conflicting or unavailable evidence is handled without fabrication - `src/policy_engine.py`, cases DS-06, DS-07, DS-09, FX-02, FX-07.
- [x] Human approval is preserved for sensitive decisions - `human_review_required` is always true; decisions are recorded by a person in the UI.
- [x] Prompt injection inside business data does not override system behaviour - approvals are computed in code; cases DS-06, FX-01, FX-04; `tests/test_pipeline.py`.
- [x] Date-based checks use the policy's reference date - `data_access.load_reference_date`; `tests/test_policy_engine.py`.
- [x] The same evaluation cases were run on both architectures - `evals/run_comparison.py`.
- [x] Latency and LLM/tool-call counts are reported - `evals/results/<model>/summary.md` and `results.csv`.
- [x] The decision memo is at most 500 words and supported by evaluation evidence - `docs/architecture_decision.md`.
- [x] Setup instructions work from a clean environment - README "Quick start".
- [x] No provider SDK is required - `src/llm.py` calls the OpenAI-compatible HTTP API with `httpx`, which is in `requirements.txt`.
- [x] `.env`, API keys and other secrets are not committed - `.gitignore`; `.env.example` provided.
