"""
sitecustomize.py — APE self-invocation shim
============================================
This file is automatically imported by Python at startup (before any user
code runs) because it lives at Lib/sitecustomize.py inside the binary's ZIP
layer, which is on sys.path.

Problem it solves
-----------------
An APE (Actually Portable Executable) binary IS the Python interpreter.  When
the OS runs ./setup-dev-env.com or ./developer-daemon.com natively (as ELF on
Linux, Mach-O on macOS, or PE on Windows), Python starts with no script
argument and opens an interactive REPL instead of running the embedded
__main__.py.

Python only executes a ZIP's __main__.py when the ZIP is passed as the
*script* argument:

    python-interpreter  zipapp.zip  [args...]

But here the interpreter and the zipapp are the same file, so there is no
separate script path to supply.

Solution
--------
This module detects the "bare invocation" condition (sys.argv == ['']),
confirms that a __main__.py is accessible via zipimport from sys.executable,
and then calls os.execv to restart the process with the executable passed as
BOTH the interpreter AND the script:

    os.execv(exe, [exe, exe] + extra_args)

On the second pass Python sees exe as the script path, treats it as a
zipapp, finds __main__.py, and runs it normally.  sys.argv inside __main__.py
becomes [exe, *extra_args], which is exactly what argparse expects.

Flag relay via environment variable
-------------------------------------
Python's own argument parser intercepts unknown flags (e.g. --no-tests) and
rejects them with an error *before* sitecustomize.py is even imported.  Flags
therefore cannot be passed directly on the command line during a bare
invocation.

As a workaround, flags can be relayed through the _APE_ARGS environment
variable:

    _APE_ARGS="--no-tests --no-blast" ./setup-dev-env.com

sitecustomize reads _APE_ARGS, splits it on whitespace, and appends the
tokens to the re-exec argv so __main__.py receives them as normal sys.argv
entries.  The variable is removed from os.environ before the re-exec so it
is not visible to the bootstrap code.

Alternatively, pass the binary as its own first argument (always works and
supports all flags directly, bypassing this shim entirely):

    ./setup-dev-env.com ./setup-dev-env.com --no-tests --no-blast

Safety guards
-------------
* Only triggers when sys.argv == ['']  (truly bare — no user script passed).
* Verifies __main__.py is actually importable via zipimport from the
  executable before doing anything, so plain `python.com` invocations that
  happen to have no arguments are never affected.
* Pops _APE_ARGS from os.environ before re-execing so bootstrap code never
  sees it.
* Wraps the os.execv call in a try/except; if it fails (e.g. exotic read-only
  filesystem), Python falls through to its normal REPL rather than crashing
  with an unhelpful traceback.
* Deletes every name introduced here before returning so the global namespace
  seen by __main__.py is completely clean.
"""

import os as _os
import sys as _sys

# Name of the environment variable used to relay flags across the re-exec.
# e.g.:  _APE_ARGS="--no-tests --no-daemon" ./setup-dev-env.com
_APE_ARGS_ENV = "_APE_ARGS"


def _auto_run() -> None:
    """Re-exec this binary as a zipapp when invoked bare (no script argument)."""

    # sys.argv == [''] is the canonical state when Python was started with no
    # script: `./setup-dev-env.com` on all platforms.
    if _sys.argv != [""]:
        return

    exe = _sys.executable
    if not exe:
        return

    # Verify there is a __main__.py in this binary's ZIP layer before we
    # commit to a re-exec.  This prevents accidentally hijacking a plain
    # `python.com` invocation that happens to have no arguments.
    try:
        import zipimport as _zi

        _loader = _zi.zipimporter(exe)
        if _loader.get_code("__main__") is None:
            return
    except Exception:
        return

    # Collect any flags relayed by the caller through the environment variable.
    # Split on whitespace — sufficient for our bootstrap flags (--no-tests,
    # --no-blast, --no-daemon, etc.) which never contain spaces themselves.
    _relay_raw = _os.environ.pop(_APE_ARGS_ENV, "").strip()
    _extra = _relay_raw.split() if _relay_raw else []

    # Replace this process image.
    #
    #   argv[0]  = exe   — the interpreter path (kept as-is by the OS)
    #   argv[1]  = exe   — Python sees this as the *script* path and opens it
    #                      as a zipapp, finding and running __main__.py
    #   argv[2+] = extra — any relayed flags, visible as sys.argv[1:] inside
    #                      __main__.py
    #
    # os.execv never returns on success; the current process image is replaced.
    try:
        _os.execv(exe, [exe, exe] + _extra)
    except OSError:
        # execv failed (unusual — e.g. the binary is on a noexec mount).
        # Fall through silently; Python will open a REPL, which is better
        # than an opaque crash.
        pass


_auto_run()

# Remove every name this module introduced so that __main__.py starts with a
# completely clean global namespace, as if sitecustomize.py never existed.
del _auto_run
del _APE_ARGS_ENV
del _os
del _sys
