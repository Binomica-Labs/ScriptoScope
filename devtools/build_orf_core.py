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

# -O3 / -march=native: enable full optimisation and NEON/SSE2 intrinsics for
# the current CPU.  -std=c11 makes C11 _Atomic / stdatomic.h available.
# On Windows the equivalent MSVC flags are /O2 and /std:c11.
if sys.platform != "win32":
    EXTRA_COMPILE_ARGS = ["-O3", "-march=native", "-std=c11"]
else:
    EXTRA_COMPILE_ARGS = ["/O2", "/std:c11"]


def main() -> int:
    cmd = [
        sys.executable,
        "-c",
        "from setuptools import setup, Extension; "
        "setup("
        "  name='orf_core',"
        "  ext_modules=[Extension('orf_core', ['orf_core.c'],"
        f"    extra_compile_args={EXTRA_COMPILE_ARGS!r})],"
        "  script_args=['build_ext', '--inplace']"
        ")",
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        # Find the built shared library and report its name.
        for p in ROOT.glob("orf_core*.so"):
            print(f"\nBuilt: {p.name}")
        for p in ROOT.glob("orf_core*.pyd"):
            print(f"\nBuilt: {p.name}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
