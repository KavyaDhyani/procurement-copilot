"""Shared plumbing for the evaluation scripts."""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import uvicorn


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
    A local URL that is not answering gets an in-process server; a remote one is an error.
    """
    if risk_file is not None:
        os.environ["MOCK_VENDOR_RISK_FILE"] = str(risk_file)
    if _healthy(base_url):
        if risk_file is not None:
            raise RuntimeError(f"{base_url} is already serving another dataset; choose a free port for the fixture API.")
        yield "already running"
        return
    parsed = urlparse(base_url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError(f"Vendor-risk API at {base_url} is not reachable.")
    from mock_api.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=parsed.port or 80, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if _healthy(base_url):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError(f"Could not start the mock vendor-risk API on {base_url} (is the port in use?)")
    try:
        yield "started for this run"
    finally:
        server.should_exit = True
        os.environ.pop("MOCK_VENDOR_RISK_FILE", None)
