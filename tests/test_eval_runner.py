"""The comparison runner's handling of recorded runs. Recorded model output costs quota to recreate,
so no mode may lose it. (A replay once truncated the live record before reading it; these tests pin that down.)"""
from __future__ import annotations

import json
import socket
import sys

import pytest

from evals import run_comparison
from src import data_access
from src.solution import analyze_request
from tests.test_pipeline import script


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def recorded(tmp_path, api_url, settings, scripted_llm, monkeypatch):
    """An output folder holding one recorded 'live' run, produced with a scripted model."""
    req = data_access.get_request("REQ-1008")
    duplicate = {"assessment": "likely_duplicate", "existing_products": ["TaskFlow (SW003)"], "reason": "Licensed company-wide."}
    result = analyze_request(req, "single", settings, scripted_llm(script(req, "single", "review_existing_tool_first", overlap=duplicate)))
    case = next(c for c in json.loads((run_comparison.ROOT / "evals" / "cases.json").read_text(encoding="utf-8")) if c["case_id"] == "DS-08")
    seed = {"model": "scripted", "wall_ms": 1234, "result": result.model_dump(mode="json")}
    row = run_comparison.run_one(case, req, "single", settings, 1, {}, live_row=seed)
    # pretend the live run, on older code, had scored this case as a wrongly waved-through failure
    row |= {"passed": False, "action": "proceed_to_standard_approval", "correct_next_action": False}
    out = tmp_path / "results"
    out.mkdir()
    (out / "runs.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.setenv("VENDOR_RISK_BASE_URL", api_url)
    monkeypatch.setenv("MODEL_NAME", "scripted")
    monkeypatch.setattr(run_comparison, "FIXTURE_API", f"http://127.0.0.1:{free_port()}")
    return out, row


def run_cli(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["run_comparison.py", *args])
    run_comparison.main()


def test_replay_keeps_the_live_record_and_rescored_copy(recorded, monkeypatch):
    out, row = recorded
    original = (out / "runs.jsonl").read_text(encoding="utf-8")
    run_cli(monkeypatch, "--replay", "--out", str(out))

    assert (out / "runs_live.jsonl").read_text(encoding="utf-8") == original          # live record untouched
    replayed = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(replayed) == 1 and replayed[0]["passed"] and replayed[0]["action"] == "review_existing_tool_first"
    assert replayed[0]["wall_ms"] == 1234                                             # live measurements carried over
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "live proceed_to_standard_approval FAIL -> now review_existing_tool_first PASS" in summary

    run_cli(monkeypatch, "--replay", "--out", str(out))                               # replaying again starts from the live record
    assert (out / "runs_live.jsonl").read_text(encoding="utf-8") == original


def test_report_only_does_not_modify_recorded_runs(recorded, monkeypatch):
    out, _ = recorded
    original = (out / "runs.jsonl").read_text(encoding="utf-8")
    run_cli(monkeypatch, "--report-only", "--out", str(out))
    assert (out / "runs.jsonl").read_text(encoding="utf-8") == original
    assert (out / "results.csv").is_file()


def test_fresh_live_run_refuses_to_discard_recorded_runs(recorded, monkeypatch):
    out, _ = recorded
    original = (out / "runs.jsonl").read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        run_cli(monkeypatch, "--out", str(out), "--cases", "DS-08")
    assert "already holds 1 recorded runs" in str(stop.value)
    assert (out / "runs.jsonl").read_text(encoding="utf-8") == original


def test_replay_with_nothing_recorded_fails_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "scripted")
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "--replay", "--out", str(tmp_path / "empty"))
    assert not (tmp_path / "empty" / "runs_live.jsonl").exists()
