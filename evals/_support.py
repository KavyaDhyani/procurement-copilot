"""Shared plumbing for the evaluation scripts."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]


def _healthy(base_url: str) -> bool:
    try:
        return requests.get(base_url.rstrip("/") + "/health", timeout=1.0).ok
    except requests.RequestException:
        return False


@contextlib.contextmanager
def vendor_api(base_url: str, risk_file: Path | None = None):
    """Make sure a vendor-risk API is answering at `base_url` for the duration of an evaluation.

    Without this, a forgotten `python run_local.py` makes every case degrade to
    "vendor risk unavailable" and the results look like a model failure.

    - default dataset (`risk_file` is None): reuse a running API, otherwise start one;
    - fixture dataset: always start a dedicated API serving `risk_file`, so the port must be free.
    Yields a short description of what was done.
    """
    if _healthy(base_url):
        if risk_file is not None:
            raise RuntimeError(f"{base_url} is already in use; the fixture vendor-risk API needs a free port.")
        yield "already running"
        return
    parsed = urlparse(base_url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError(f"Vendor-risk API at {base_url} is not reachable.")
    env = dict(os.environ)
    if risk_file is not None:
        env["MOCK_VENDOR_RISK_FILE"] = str(risk_file)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1", "--port", str(parsed.port or 80),
         "--log-level", "warning"], cwd=ROOT, env=env)
    try:
        for _ in range(100):
            if _healthy(base_url):
                break
            if proc.poll() is not None:
                raise RuntimeError(f"Mock vendor-risk API exited on startup (is port {parsed.port} in use?)")
            time.sleep(0.1)
        else:
            raise RuntimeError(f"Mock vendor-risk API did not become ready on {base_url}")
        yield "started for this run"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
