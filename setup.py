"""
setup.py — C extension declaration for orf_core.

This file exists solely to declare the orf_core C extension module so that
the standard build pipeline (pip install . / pipx install .) compiles and
installs it automatically alongside scriptoscope.py.

All other project metadata lives in pyproject.toml.  setuptools picks up
both files and merges them at build time.

Compiler flags
--------------
  -O3            maximum optimisation
  -march=native  emit NEON (Apple Silicon / ARM) or SSE2/AVX2 (x86) intrinsics
                 for the SIMD uppercase pass in orf_core.c
  -std=c11       required for <stdatomic.h> (_Atomic + atomic_fetch_add) used
                 by the work-stealing thread pool
"""

import sys

from setuptools import Extension, setup

if sys.platform == "win32":
    extra_compile_args = ["/O2", "/std:c11"]
else:
    extra_compile_args = ["-O3", "-march=native", "-std=c11"]

setup(
    ext_modules=[
        Extension(
            "orf_core",
            sources=["orf_core.c"],
            extra_compile_args=extra_compile_args,
        ),
    ],
)
