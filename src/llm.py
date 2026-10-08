"""Minimal client for any OpenAI-compatible chat-completions endpoint (Groq by default).

Deliberately SDK-free: the copilot needs exactly one operation - "return JSON
matching this schema" - plus control over rate-limit behaviour, which matters on
free tiers (Groq: 8,000 tokens/minute). The client paces itself from the
provider's rate-limit headers and reports any time spent waiting separately, so
latency comparisons between architectures are not distorted by quota waits.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from src.models import Usage

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "keys": ("GROQ_API_KEY", "GROQ_KEY"),
             "model_env": "GROQ_MODEL", "default_model": "openai/gpt-oss-120b"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "keys": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
               "model_env": "GEMINI_MODEL", "default_model": "gemini-3.8-flash"},
}


class LLMError(RuntimeError):
    """The model could not produce a usable answer (not configured, rate-limited, down, or invalid output)."""


class JsonLLM(Protocol):
    model: str

    def complete_json(self, system: str, user: str, schema_name: str, schema: dict, usage: Usage) -> dict: ...


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str | None
    max_rate_limit_wait_seconds: float
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "LLMSettings":
        env = lambda name: (os.getenv(name) or "").strip()
        provider = env("LLM_PROVIDER").lower()
        if not provider:
            if env("LLM_API_KEY") and env("LLM_BASE_URL"):
                provider = "custom"
            else:
                provider = next((p for p, cfg in PROVIDERS.items() if any(env(k) for k in cfg["keys"])), "")
        if provider == "custom":
            base_url, api_key, model = env("LLM_BASE_URL"), env("LLM_API_KEY"), env("MODEL_NAME")
        elif provider in PROVIDERS:
            cfg = PROVIDERS[provider]
            base_url = env("LLM_BASE_URL") or cfg["base_url"]
            api_key = env("LLM_API_KEY") or next((env(k) for k in cfg["keys"] if env(k)), "")
            model = env("MODEL_NAME") or env(cfg["model_env"]) or cfg["default_model"]
        else:
            raise LLMError("No model provider configured. Set GROQ_API_KEY (or GEMINI_API_KEY, or LLM_BASE_URL + "
                           "LLM_API_KEY + MODEL_NAME) in .env - see .env.example.")
        if not (base_url and api_key and model):
            raise LLMError(f"Model provider '{provider}' is missing a base URL, API key or model name - see .env.example.")
        effort = env("LLM_REASONING_EFFORT") or ("low" if "gpt-oss" in model else "")
        return cls(provider=provider, base_url=base_url.rstrip("/"), api_key=api_key, model=model,
                   reasoning_effort=effort or None,
                   max_rate_limit_wait_seconds=float(env("LLM_MAX_RATE_LIMIT_WAIT_SECONDS") or 75),
                   timeout_seconds=float(env("LLM_TIMEOUT_SECONDS") or 60))


def _seconds(value: str | None) -> float | None:
    """Parse '6.09s', '1m26.4s', '120ms' or a bare number of seconds."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    total, matched = 0.0, False
    for number, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        total += float(number) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
        matched = True
    return total if matched else None


class _TokenBucket:
    """Last known per-minute token allowance for one (endpoint, model), shared across client instances."""

    def __init__(self) -> None:
        self.limit: float | None = None
        self.remaining: float | None = None
        self.observed_at = 0.0

    def update(self, headers: httpx.Headers) -> None:
        try:
            self.limit = float(headers["x-ratelimit-limit-tokens"])
            self.remaining = float(headers["x-ratelimit-remaining-tokens"])
            self.observed_at = time.monotonic()
        except (KeyError, ValueError):
            pass

    def wait_needed(self, tokens: float) -> float:
        if not self.limit or self.remaining is None:
            return 0.0
        refill_per_second = self.limit / 60.0
        available = min(self.limit, self.remaining + (time.monotonic() - self.observed_at) * refill_per_second)
        return max(0.0, (min(tokens, self.limit) - available) / refill_per_second)


_BUCKETS: dict[tuple[str, str], _TokenBucket] = {}


class OpenAICompatLLM:
    def __init__(self, settings: LLMSettings | None = None) -> None:
        self.settings = settings or LLMSettings.from_env()
        self.model = self.settings.model
        self._bucket = _BUCKETS.setdefault((self.settings.base_url, self.model), _TokenBucket())

    def _sleep_for_quota(self, seconds: float, usage: Usage) -> None:
        if seconds > self.settings.max_rate_limit_wait_seconds:
            raise LLMError(f"Model rate limit would require waiting {seconds:.0f}s "
                           f"(limit {self.settings.max_rate_limit_wait_seconds:.0f}s); likely a daily quota.")
        if seconds > 0:
            time.sleep(seconds)
            usage.rate_limit_wait_ms += seconds * 1000

    def complete_json(self, system: str, user: str, schema_name: str, schema: dict, usage: Usage) -> dict:
        s = self.settings
        payload: dict = {
            "model": s.model, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
        }
        if s.reasoning_effort:
            payload["reasoning_effort"] = s.reasoning_effort
        estimated_tokens = (len(system) + len(user) + len(json.dumps(schema))) / 3.2 + 700
        last_error = "no attempt made"

        for attempt in range(4):
            self._sleep_for_quota(self._bucket.wait_needed(estimated_tokens), usage)
            started = time.perf_counter()
            try:
                response = httpx.post(f"{s.base_url}/chat/completions", json=payload, timeout=s.timeout_seconds,
                                      headers={"Authorization": f"Bearer {s.api_key}"})
            except httpx.HTTPError as exc:
                usage.llm_ms += (time.perf_counter() - started) * 1000
                usage.llm_retries += 1
                last_error = f"{type(exc).__name__} calling the model endpoint"
                time.sleep(0.5 * (attempt + 1))
                continue
            usage.llm_ms += (time.perf_counter() - started) * 1000
            self._bucket.update(response.headers)

            if response.status_code == 429:
                last_error = "rate limited (HTTP 429)"
                usage.llm_retries += 1
                self._sleep_for_quota(_seconds(response.headers.get("retry-after")) or 5.0, usage)
                continue
            if response.status_code >= 500:
                last_error = f"model endpoint error (HTTP {response.status_code})"
                usage.llm_retries += 1
                time.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code != 200:
                message = _error_message(response)
                # A schema-validation miss is a sampling failure worth one more try; other 4xx are not.
                if response.status_code == 400 and "json" in message.lower() and attempt < 2:
                    last_error = f"model returned invalid JSON: {message}"
                    usage.llm_retries += 1
                    continue
                raise LLMError(f"Model request rejected (HTTP {response.status_code}): {message}")

            body = response.json()
            usage.llm_calls += 1
            tokens = body.get("usage") or {}
            usage.prompt_tokens += int(tokens.get("prompt_tokens") or 0)
            usage.completion_tokens += int(tokens.get("completion_tokens") or 0)
            try:
                parsed = json.loads(body["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError, ValueError):
                last_error = "model returned content that is not valid JSON"
                usage.llm_retries += 1
                continue
            if isinstance(parsed, dict):
                return parsed
            last_error = "model returned JSON that is not an object"
        raise LLMError(f"Model call failed after retries: {last_error}")


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", response.text))[:300]
    except (ValueError, AttributeError):
        return response.text[:300]
