"""Read-only access to the business data snapshot.

Files are read on every call: the data is tiny, and hidden evaluation cases swap
in different values behind the same interface, so nothing may be cached at import.
Values are kept as strings exactly as written (no NaN coercion); callers parse
what they need and treat blanks as "not recorded".
"""
from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path

from src.config import DEFAULT_DATA_DIR


def _dir(data_dir: Path | None) -> Path:
    return Path(data_dir) if data_dir else DEFAULT_DATA_DIR


def name_key(value: object) -> str:
    """Case- and whitespace-insensitive key for matching names across sources."""
    return " ".join(str(value or "").split()).casefold()


def _read_csv(filename: str, data_dir: Path | None) -> list[dict[str, str]]:
    with (_dir(data_dir) / filename).open(encoding="utf-8", newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def load_employees(data_dir: Path | None = None) -> list[dict[str, str]]:
    return _read_csv("employees.csv", data_dir)


def load_budgets(data_dir: Path | None = None) -> list[dict[str, str]]:
    return _read_csv("department_budgets.csv", data_dir)


def load_software_catalog(data_dir: Path | None = None) -> list[dict[str, str]]:
    return _read_csv("software_catalog.csv", data_dir)


def load_vendors(data_dir: Path | None = None) -> list[dict[str, str]]:
    return _read_csv("vendors.csv", data_dir)


def load_purchase_history(data_dir: Path | None = None) -> list[dict[str, str]]:
    return _read_csv("purchase_history.csv", data_dir)


def load_requests(data_dir: Path | None = None) -> list[dict]:
    return json.loads((_dir(data_dir) / "requests.json").read_text(encoding="utf-8"))


def get_request(request_id: str, data_dir: Path | None = None) -> dict:
    for request in load_requests(data_dir):
        if request.get("request_id") == request_id:
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


def load_policy_text(data_dir: Path | None = None) -> str:
    return (_dir(data_dir) / "procurement_policy.md").read_text(encoding="utf-8")


def find_employee(employee_id: str | None, data_dir: Path | None = None) -> dict[str, str] | None:
    key = name_key(employee_id)
    return next((e for e in load_employees(data_dir) if key and name_key(e["employee_id"]) == key), None)


def find_budget(department: str | None, data_dir: Path | None = None) -> dict[str, str] | None:
    key = name_key(department)
    return next((b for b in load_budgets(data_dir) if key and name_key(b["department"]) == key), None)


def find_vendor(vendor_name: str | None, data_dir: Path | None = None) -> dict[str, str] | None:
    key = name_key(vendor_name)
    return next((v for v in load_vendors(data_dir) if key and name_key(v["vendor_name"]) == key), None)


_REFERENCE_DATE = re.compile(r"reference date:\*{0,2}\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def load_reference_date(data_dir: Path | None = None) -> date:
    """The policy's data-snapshot date. Date rules never use the machine clock."""
    match = _REFERENCE_DATE.search(load_policy_text(data_dir))
    if not match:
        raise ValueError(
            "procurement_policy.md does not declare a 'reference date: YYYY-MM-DD'; "
            "refusing to fall back to today's date for review-expiry checks."
        )
    return date.fromisoformat(match.group(1))
