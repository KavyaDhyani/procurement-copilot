"""Entry points.

`handle_request(request_id, architecture)` is the assessment adapter used by the
evaluation harness. `analyze_request(...)` does the work and also returns the
trace (ledger, guardrail events, usage) for the UI and the comparison script.
"""
from __future__ import annotations

from typing import Callable

from src import data_access
from src.agents import single, staged
from src.agents.common import ModelDraft, ensure_policy_evaluated
from src.config import Settings
from src.contracts import Architecture, ProcurementDecision
from src.finalizer import finalize
from src.llm import JsonLLM, LLMError, OpenAICompatLLM
from src.models import AnalysisResult, ProcurementRequest, Usage
from src.tools import RunContext

AGENTS: dict[str, Callable[..., ModelDraft]] = {"single": single.run, "staged": staged.run}


def analyze_request(raw_request: dict, architecture: Architecture = "single", settings: Settings | None = None,
                    llm: JsonLLM | None = None) -> AnalysisResult:
    """Analyse one purchase request end to end. Never raises for model or vendor-service failures."""
    if architecture not in AGENTS:
        raise ValueError(f"Unknown architecture '{architecture}'; expected one of {sorted(AGENTS)}")
    settings = settings or Settings.from_env()
    ctx = RunContext(request=ProcurementRequest.from_raw(raw_request), settings=settings,
                     reference_date=data_access.load_reference_date(settings.data_dir))
    usage, stages = Usage(), []
    draft: ModelDraft | None = None
    degraded_reason: str | None = None
    model_output_invalid = False
    model: str | None = None
    try:
        llm = llm or OpenAICompatLLM()
        model = llm.model
        draft = AGENTS[architecture](ctx, llm, usage, stages)
    except LLMError as exc:
        degraded_reason = str(exc)
        model_output_invalid = exc.invalid_output
    except Exception as exc:  # an orchestration bug must not take the deterministic checks down with it
        degraded_reason = f"Agent failed unexpectedly ({type(exc).__name__}: {exc})"
        model_output_invalid = True

    # Whatever the model did or did not do, the mandatory checks and the policy engine run.
    ensure_policy_evaluated(ctx)
    result = finalize(ctx, draft, architecture, usage, stages, degraded_reason=degraded_reason, model=model)
    result.model_output_invalid = model_output_invalid
    return result


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter: look the request up by id and return the contract object."""
    settings = Settings.from_env()
    return analyze_request(data_access.get_request(request_id, settings.data_dir), architecture, settings).decision
