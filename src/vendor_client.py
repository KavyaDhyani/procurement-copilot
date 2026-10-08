"""Client for the external vendor-risk service.

The service can be down, slow, or have no record. Those are different facts for
a reviewer, so the client reports them as distinct outcomes and never raises:
"we could not check" must not be confused with "there is nothing to find".
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import requests

from src.config import Settings

Outcome = Literal["ok", "not_found", "unavailable", "invalid_response"]


@dataclass(frozen=True)
class VendorRiskResult:
    outcome: Outcome
    endpoint: str
    record: dict | None = None
    http_status: int | None = None
    detail: str = ""
    attempts: int = 1


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
        return str(body.get("detail", body))[:300] if isinstance(body, dict) else str(body)[:300]
    except ValueError:
        return response.text[:300]


def fetch_vendor_risk(vendor_name: str, settings: Settings, retries: int = 1) -> VendorRiskResult:
    """GET /vendor-risk/{vendor}. Transient failures (5xx, timeouts) are retried once."""
    path = f"/vendor-risk/{quote(vendor_name, safe='')}"
    url = settings.vendor_risk_base_url + path
    endpoint = f"GET {path}"
    timeout = (min(1.5, settings.vendor_risk_timeout_seconds), settings.vendor_risk_timeout_seconds)
    last = VendorRiskResult("unavailable", endpoint, detail="not attempted")

    for attempt in range(1, retries + 2):
        try:
            response = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last = VendorRiskResult("unavailable", endpoint, detail=f"{type(exc).__name__}: service unreachable", attempts=attempt)
        else:
            status = response.status_code
            if status == 200:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and "security_review_status" in body:
                    return VendorRiskResult("ok", endpoint, record=body, http_status=status, attempts=attempt)
                return VendorRiskResult(
                    "invalid_response", endpoint, http_status=status,
                    detail="Response did not contain a vendor-risk record", attempts=attempt,
                )
            if status == 404:
                return VendorRiskResult("not_found", endpoint, http_status=status, detail=_detail(response), attempts=attempt)
            last = VendorRiskResult("unavailable", endpoint, http_status=status, detail=_detail(response), attempts=attempt)
            if status < 500:
                return last
        if attempt <= retries:
            time.sleep(0.2)
    return last
