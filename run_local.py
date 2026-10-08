"""One-command local start:  python run_local.py

Starts the mock vendor-risk API and the copilot UI at fixed addresses:

    UI   http://127.0.0.1:8501     <- open this one in the browser
    API  http://127.0.0.1:8001     (mock vendor-risk service used by the copilot's tools)

If either port is taken the launcher stops and says so; it never moves to another port.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


if __name__ == "__main__":
    # Started from another Python (e.g. a conda env)? Re-run under .venv, where the dependencies are.
    from project_env import use_project_virtualenv

    use_project_virtualenv(__file__)

import signal  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

try:
    import requests
    from dotenv import load_dotenv
except ImportError as exc:  # no .venv to switch to, and this interpreter lacks the dependencies
    raise SystemExit(
        f"Missing dependency ({exc.name}) in {sys.executable}.\n"
        "Set the project up once, from this folder:\n"
        "    python -m venv .venv\n"
        "    .venv/bin/python -m pip install -r requirements.txt      (Windows: .venv\\Scripts\\python ...)\n"
        "then run:  python run_local.py"
    )

load_dotenv(ROOT / ".env", override=False)

# The UI and the mock API must agree on one address, so both are derived from VENDOR_RISK_BASE_URL.
API_URL = (os.getenv("VENDOR_RISK_BASE_URL") or "http://127.0.0.1:8001").rstrip("/")
API_HOST = urlparse(API_URL).hostname or "127.0.0.1"
API_PORT = str(urlparse(API_URL).port or 8001)
# Fixed UI address. UI_PORT exists only as an escape hatch for a port conflict; it is never chosen automatically.
UI_PORT = os.getenv("UI_PORT") or "8501"
UI_URL = f"http://127.0.0.1:{UI_PORT}"


def start(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(cmd, cwd=ROOT)


def port_in_use(port: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", int(port))) == 0


def api_is_up() -> bool:
    try:
        return requests.get(f"{API_URL}/health", timeout=0.5).ok
    except requests.RequestException:
        return False


def describe_model() -> str:
    from src.llm import LLMError, LLMSettings

    try:
        settings = LLMSettings.from_env()
    except LLMError as exc:
        return f"NOT CONFIGURED - the copilot will return deterministic checks only. {exc}"
    return f"{settings.provider} / {settings.model}"


def wait_until_ready(name: str, url: str, proc: subprocess.Popen, timeout_seconds: float) -> None:
    """Wait until `url` answers, or fail with a useful message."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited during startup with code {proc.returncode}. Check the output above.")
        try:
            if requests.get(url, timeout=0.5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{name} did not become ready within {timeout_seconds:.0f}s: {url}")


def _handle_termination(signum: int, frame: object) -> None:
    """Route SIGTERM through normal cleanup (useful for IDE/terminal stop actions)."""
    raise KeyboardInterrupt


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_termination)
    procs: list[subprocess.Popen] = []
    try:
        try:
            __import__("streamlit")
        except ImportError:
            raise RuntimeError(
                f"The UI cannot start: Streamlit is not installed in {sys.executable}.\n"
                "Install the project's dependencies with:  python -m pip install -r requirements.txt"
            )
        if port_in_use(UI_PORT):
            raise RuntimeError(
                f"Port {UI_PORT} is already in use, so the UI cannot start at {UI_URL}.\n"
                "Stop whatever is using it (an earlier run of this launcher is the usual cause), "
                "or set UI_PORT to another port for this run."
            )

        print(f"Model: {describe_model()}")
        if api_is_up():
            print(f"Vendor-risk API already running on {API_URL}; reusing it.")
        elif API_HOST not in ("127.0.0.1", "localhost"):
            raise RuntimeError(f"VENDOR_RISK_BASE_URL points at {API_URL}, which is not reachable and is not local.")
        elif port_in_use(API_PORT):
            raise RuntimeError(f"Port {API_PORT} is in use by something that is not the vendor-risk API. Free it and retry.")
        else:
            print(f"Starting vendor-risk API on {API_URL} ...")
            api_proc = start([sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", API_HOST, "--port", API_PORT,
                              "--log-level", "warning"])
            procs.append(api_proc)
            wait_until_ready("Vendor-risk API", f"{API_URL}/health", api_proc, timeout_seconds=15)

        print(f"Starting the copilot UI on {UI_URL} ...")
        ui_proc = start([
            sys.executable, "-m", "streamlit", "run", "app.py",
            "--server.port", UI_PORT,
            "--server.address", "127.0.0.1",
            # headless: never block on Streamlit's first-run e-mail prompt, never open a browser by itself
            "--server.headless", "true",
            "--browser.gatherUsageStats", "false",
        ])
        procs.append(ui_proc)
        wait_until_ready("Copilot UI", f"{UI_URL}/_stcore/health", ui_proc, timeout_seconds=60)

        print("\n" + "=" * 62)
        print(f"  Procurement Request Copilot is ready:  {UI_URL}")
        print(f"  (mock vendor-risk API: {API_URL} - not a web page)")
        print("  Press Ctrl+C to stop.")
        print("=" * 62 + "\n", flush=True)

        while True:
            time.sleep(1)
            for proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"A local process exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\nStopping local services ...")
        return 0
    except RuntimeError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
