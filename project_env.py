"""Makes the project's scripts run under the project's own virtualenv, whichever Python started them.

The dependencies are installed in `.venv`. Started from another interpreter (a conda env, the system
Python) a script would find packages missing or at the wrong versions, so it re-runs itself with
`.venv`'s Python instead. Standard library only: this is imported before any third-party package.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def project_python(prefix: str | None = None) -> Path | None:
    """The .venv interpreter to switch to, or None if we are already in it, it does not exist, or switching is off."""
    venv = ROOT / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if os.getenv("COPILOT_NO_VENV_SWITCH") or not python.is_file():
        return None
    if Path(prefix or sys.prefix).resolve() == venv.resolve():
        return None
    return python


def use_project_virtualenv(script: str) -> None:
    """Call first thing in a script's `__main__` path. Returns normally if no switch is needed."""
    python = project_python()
    if python is None:
        return
    print(f"Switching to the project environment: {python}", flush=True)
    command = [str(python), str(Path(script).resolve()), *sys.argv[1:]]
    if os.name == "nt":
        raise SystemExit(subprocess.call(command))
    os.execv(str(python), command)
