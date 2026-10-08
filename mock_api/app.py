from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).resolve().parents[1]

app = FastAPI(title="FDE Mock Vendor Risk API", version="1.1")


def _load() -> dict:
    """Read the backing file per request so an edited or swapped snapshot is served without a restart.

    MOCK_VENDOR_RISK_FILE lets the evaluation harness serve a fixture dataset.
    """
    path = Path(os.getenv("MOCK_VENDOR_RISK_FILE") or ROOT / "data" / "vendor_risk.json")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# `:path` so vendor names containing "/" still reach the handler. The framework has
# already URL-decoded the value; decoding again would corrupt names containing "%".
@app.get("/vendor-risk/{vendor_name:path}")
def vendor_risk(vendor_name: str) -> dict:
    name = vendor_name.strip()
    record = _load().get(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No vendor-risk record for '{name}'")
    if record.get("simulate_delay_seconds"):
        time.sleep(float(record["simulate_delay_seconds"]))
    if record.get("force_error"):
        raise HTTPException(status_code=503, detail=record.get("error_message", "Vendor-risk service unavailable"))
    return {"vendor_name": name, **{k: v for k, v in record.items() if k != "simulate_delay_seconds"}}
