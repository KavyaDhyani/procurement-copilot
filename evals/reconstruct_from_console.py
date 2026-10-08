"""Rebuild results.csv and summary.md from the console log of a live comparison run.

    python evals/reconstruct_from_console.py evals/results/<model>/live_console.log

For a run whose detailed record (runs.jsonl) is not available. The console prints one line per run with the
outcome, action, processing latency, call counts and tokens, and a second line with the scorer's notes when a
run failed. Everything in the report comes from those lines; counters the console does not print are shown
as "not recorded" rather than estimated.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.run_comparison import GROUNDING_EVENTS, write_reports  # noqa: E402

RUN = re.compile(r"^(PASS|FAIL)\s+(\S+) \[(\w+)\s*\] (\w+)\s+(\d+) ms\s+(\d+) llm\s+(\d+) tools\s+(\d+) tok(?:\s+\(waited (\d+)s for quota\))?")


def parse(log: Path) -> tuple[str, list[dict]]:
    model, rows = "unknown", []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.startswith("Model: "):
            model = line.split("Model: ", 1)[1].strip()
        elif match := RUN.match(line):
            outcome, case_id, arch, action, ms, llm, tools, tokens, waited = match.groups()
            rows.append({"case_id": case_id, "architecture": arch, "trial": 1, "model": model, "degraded": False,
                         "passed": outcome == "PASS", "action": action, "latency_ms": int(ms), "llm_calls": int(llm),
                         "tool_calls": int(tools), "tokens": int(tokens), "rate_limit_wait_ms": int(waited or 0) * 1000,
                         "notes": "", "correct_next_action": True, "grounded_evidence": True, "policy_followed": True,
                         "human_escalation_correct": True})
        elif rows and line.startswith("      ") and not rows[-1]["passed"] and not rows[-1]["notes"]:
            notes = line.strip()
            rows[-1].update(
                notes=notes,
                correct_next_action=not notes.startswith("action ") and "; action " not in notes,
                grounded_evidence=not any(f"{kind}:" in notes for kind in GROUNDING_EVENTS),
                policy_followed=not re.search(r"approvals [+-]|flags missing|unexpected flags|missing-info", notes),
                human_escalation_correct="escalation:" not in notes,
            )
    return model, rows


def main() -> None:
    log = Path(sys.argv[1])
    model, rows = parse(log)
    cases = [c for c in json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
             if any(r["case_id"] == c["case_id"] for r in rows)]
    provenance = [
        f"Reconstructed from `{log.name}`, the console output of the live run, because the detailed per-run record for this "
        "model was lost (an early version of the replay mode truncated it; the runner now refuses to discard recorded runs "
        "and has tests for that). Outcome, action, latency, call counts, tokens and the scorer's notes on failures are "
        "exactly as printed during the run. Counters the console does not print are marked \"not recorded\". These runs "
        "cannot be replayed; `python evals/run_comparison.py --overwrite` regenerates a full record for this model.",
    ]
    write_reports(log.parent, rows, cases, model, provenance)
    print(f"Rebuilt summary.md and results.csv in {log.parent} from {len(rows)} console lines ({model})")


if __name__ == "__main__":
    main()
