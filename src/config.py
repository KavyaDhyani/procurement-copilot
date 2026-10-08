"""Runtime settings.

Everything is resolved at call time (not import time) so the evaluation harness
can point the same code at a different data snapshot or vendor-risk endpoint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_VENDOR_RISK_BASE_URL = "http://127.0.0.1:8001"


@dataclass(frozen=True)
class Settings:
    data_dir: Path = DEFAULT_DATA_DIR
    vendor_risk_base_url: str = DEFAULT_VENDOR_RISK_BASE_URL
    vendor_risk_timeout_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("PROCUREMENT_DATA_DIR") or DEFAULT_DATA_DIR),
            vendor_risk_base_url=(os.getenv("VENDOR_RISK_BASE_URL") or DEFAULT_VENDOR_RISK_BASE_URL).rstrip("/"),
            vendor_risk_timeout_seconds=float(os.getenv("VENDOR_RISK_TIMEOUT_SECONDS") or 3.0),
        )
