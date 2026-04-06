#!/usr/bin/env python3
"""Build the orf_core C extension in-place.

Usage:
    python3 devtools/build_orf_core.py

Produces orf_core.<platform>.so in the project root, importable as:
    import orf_core
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    cmd = [
        sys.executable, "-c",
        "from setuptools import setup, Extension; "
        "setup("
        "  name='orf_core',"
        "  ext_modules=[Extension('orf_core', ['orf_core.c'])],"
        "  script_args=['build_ext', '--inplace']"
        ")",
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        # Find the built .so
        for p in ROOT.glob("orf_core*.so"):
            print(f"\nBuilt: {p.name}")
        for p in ROOT.glob("orf_core*.pyd"):
            print(f"\nBuilt: {p.name}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
