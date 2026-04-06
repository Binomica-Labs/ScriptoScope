#!/usr/bin/env python3
"""Run the test suite against the pipx-installed ScriptoScope venv.

Usage:
    python3 devtools/run_tests.py [PYTEST_ARGS...]

Examples:
    python3 devtools/run_tests.py                    # run all tests
    python3 devtools/run_tests.py -q                 # quiet output
    python3 devtools/run_tests.py tests/test_dna_sanity.py -v
    python3 devtools/run_tests.py -k "test_random"   # filter by name
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _find_pipx_venv_python() -> Path | None:
    """Locate the pipx venv Python for scriptoscope."""
    if platform.system() == "Windows":
        base = Path(os.environ.get("USERPROFILE", "~")) / ".local" / "pipx" / "venvs" / "scriptoscope" / "Scripts"
        candidate = base / "python.exe"
    else:
        base = Path.home() / ".local" / "pipx" / "venvs" / "scriptoscope" / "bin"
        candidate = base / "python"
    return candidate if candidate.exists() else None


def main() -> int:
    venv_python = _find_pipx_venv_python()
    if venv_python is None:
        print("ERROR: Could not find pipx venv for scriptoscope.", file=sys.stderr)
        print("Install it with: pipx install . && pipx inject scriptoscope pytest pytest-asyncio", file=sys.stderr)
        return 1

    # Build the pytest command, running from the repo root so tests/
    # can import scriptoscope.py from the working tree (not the installed copy).
    pytest_args = sys.argv[1:] if len(sys.argv) > 1 else ["tests/", "-q"]
    # Add default test path only if no test path was given.
    has_path = any(a.startswith("tests") or a.endswith(".py") for a in pytest_args)
    cmd = [str(venv_python), "-m", "pytest"] + ([] if has_path else ["tests/"]) + pytest_args

    print(f"Python : {venv_python}")
    print(f"Command: {' '.join(cmd)}")
    print()

    result = subprocess.run(cmd, cwd=ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
