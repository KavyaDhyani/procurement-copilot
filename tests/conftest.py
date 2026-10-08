"""Shared fixtures: a real mock vendor-risk API on a free port, and a scripted stand-in for the model."""
from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

from mock_api.app import app
from src.config import DEFAULT_DATA_DIR, Settings
from src.llm import LLMError


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    """No test may reach a real model, whatever keys the developer's .env holds.

    Tests that need model behaviour pass a scripted model explicitly; everything else sees "no provider".
    """
    monkeypatch.setenv("LLM_PROVIDER", "none")
    for name in ("MODEL_NAME", "LLM_API_KEY", "LLM_BASE_URL", "LLM_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def api_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(url + "/health", timeout=0.5).ok:
                break
        except requests.RequestException:
            time.sleep(0.05)
    yield url
    server.should_exit = True


@pytest.fixture
def settings(api_url: str, monkeypatch) -> Settings:
    monkeypatch.delenv("MOCK_VENDOR_RISK_FILE", raising=False)
    return Settings(data_dir=DEFAULT_DATA_DIR, vendor_risk_base_url=api_url)


@pytest.fixture
def scratch_data(tmp_path: Path, api_url: str, monkeypatch):
    """A private copy of the data snapshot that a test can edit; the mock API serves its vendor_risk.json."""
    data_dir = tmp_path / "data"
    shutil.copytree(DEFAULT_DATA_DIR, data_dir)
    monkeypatch.setenv("MOCK_VENDOR_RISK_FILE", str(data_dir / "vendor_risk.json"))

    class Scratch:
        dir = data_dir
        settings = Settings(data_dir=data_dir, vendor_risk_base_url=api_url)

        @staticmethod
        def append_csv(name: str, line: str) -> None:
            with (data_dir / name).open("a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")

        @staticmethod
        def set_risk(vendor: str, record: dict | None) -> None:
            path = data_dir / "vendor_risk.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if record is None:
                data.pop(vendor, None)
            else:
                data[vendor] = record
            path.write_text(json.dumps(data), encoding="utf-8")

    return Scratch


class ScriptedLLM:
    """Returns pre-written JSON per schema name, so agent orchestration is testable without a model."""

    model = "scripted"

    def __init__(self, responses: dict[str, dict | list[dict] | Exception]) -> None:
        self.responses = {k: (list(v) if isinstance(v, list) else v) for k, v in responses.items()}
        self.calls: list[tuple[str, str, str]] = []

    def complete_json(self, system, user, schema_name, schema, usage):
        self.calls.append((schema_name, system, user))
        usage.llm_calls += 1
        response = self.responses.get(schema_name)
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise LLMError(f"no scripted response for {schema_name}")
        return response


@pytest.fixture
def scripted_llm():
    return ScriptedLLM
