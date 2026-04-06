# ScriptoScope Devtools — Engineering Development Log

A chronological record of every engineering decision made while building the
developer tooling in this directory. Written for future maintainers who need
to understand not just what was built, but why, and what was tried and
discarded along the way.

---

## 0. Starting Conditions

ScriptoScope was cloned from GitHub as a standard Python TUI project with
`scriptoscope.py`, `pyproject.toml`, `requirements.txt`, and a `tests/`
directory. No developer tooling existed. The first task was to create a pipx
venv and install the project's dependencies.

### First obstacle: broken build backend

The `pyproject.toml` declared:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"
```

`setuptools.backends.legacy:build` was a non-standard backend briefly added
and then removed from setuptools. It was never present in any released version
of setuptools and causes a `BackendUnavailable` error on every known toolchain.
The correct stable backend is `setuptools.build_meta`. This was patched in
`pyproject.toml` as the very first change.

The upstream repo still contains this broken value. Every automated setup path
built subsequently detects and patches it automatically before attempting any
install.

---

## 1. Iteration 1 — Per-Platform Scripts (Discarded)

**Files created:** `DEVELOPERS.md`, `setup-dev-env.py`, `install-python.sh`,
`install-python.ps1`

The initial approach followed conventional practice: one shell script for
Unix-likes, one PowerShell script for Windows, and a Python script that
required Python to already be installed.

- `install-python.sh` — POSIX shell, covered macOS (Homebrew + python.org
  PKG), Debian/Ubuntu (apt + deadsnakes PPA), Fedora/RHEL (dnf + module
  streams), Arch (pacman + AUR), Alpine (apk), openSUSE (zypper + OBS), Gentoo
  (emerge), Void (xbps), FreeBSD (pkg), OpenBSD (pkg_add), NetBSD (pkgin),
  DragonFlyBSD (dports), with pyenv as universal fallback.
- `install-python.ps1` — PowerShell, covered winget → Scoop → Chocolatey →
  official python.org EXE download fallback chain.
- `setup-dev-env.py` — Required Python 3.10+ to already be present. Installed
  pipx, fixed the build backend, ran `pipx install`, injected `requirements.txt`
  and test dependencies, ran the test suite.

**Why discarded:** The user correctly identified that having three separate
scripts that must be selected by OS is exactly the problem developer tooling
should eliminate. The goal was stated explicitly: *one executable that works on
every OS without the user needing to know which OS they are on*.

---

## 2. APE — Actually Portable Executable

**Reference:** https://justine.lol/ape.html

APE is a binary format invented by Justine Tunney for the Cosmopolitan Libc
project. A single APE file is simultaneously:

- A valid **Windows PE** executable (the `MZ` magic bytes at offset 0)
- A valid **Linux / BSD ELF** binary (encoded within the PE structure)
- A valid **macOS Mach-O** fat binary (x86_64 + ARM64)
- A valid **POSIX shell script** (the APE preamble is legal `sh` syntax)
- A valid **PKZIP archive** (ZIP appends its central directory at end-of-file,
  which does not conflict with any of the above)

Each OS loader reads the bytes it understands and ignores the rest. The result
is a single file that runs natively on every supported platform with no
interpreter, no runtime, and no installation.

### The Cosmopolitan Python binary

The cosmo.zip project (https://cosmo.zip/pub/cosmos/bin/) ships prebuilt APE
binaries of standard open-source tools. Among them is `python` — a ~35 MB fat
binary containing a complete CPython 3.12 interpreter, the full standard
library, and pip, compiled against Cosmopolitan Libc so it runs on all of the
above platforms without modification.

### The zipapp mechanism

Python has a standard mechanism for executable archives (PEP 441): if you pass
a ZIP file as Python's first argument, Python adds it to `sys.path` and
executes `__main__.py` from inside it. APE binaries are valid ZIP files (the
ZIP central directory is appended at the end without disturbing the binary
headers). Therefore:

```
python.com  +  zip(__main__.py)  =  self-contained executable program
```

The `zip -j output.com __main__.py` command appends `__main__.py` to the
binary's ZIP layer. When Python processes the binary as a script argument, it
finds `__main__.py` and runs it.

### First build: `setup-dev-env.com`

Build procedure (encoded in `devtools/build-ape.sh`):

1. Download `python.com` from `https://cosmo.zip/pub/cosmos/bin/python`.
   Cache it at `~/.cache/scriptoscope-ape/python.com`.
2. Copy the cached binary to `setup-dev-env.com`.
3. `zip -j setup-dev-env.com devtools/__main__.py` — appends `__main__.py`
   to the binary's ZIP layer.
4. Verify: APE magic bytes present, ZIP integrity passes, `--help` smoke test
   runs successfully.

The `__main__.py` bootstrap is pure Python stdlib — no third-party imports.
It runs inside the bundled Cosmopolitan Python before any system Python exists,
which eliminates the chicken-and-egg problem entirely.

**Deleted at this point:** `setup-dev-env.py`, `install-python.sh`,
`install-python.ps1`. All platform logic that was in those three files was
reimplemented in `devtools/__main__.py`.

---

## 3. Bootstrap Logic — `devtools/__main__.py`

The bootstrap script runs in a strict sequential pipeline. Each step is a
discrete function; failures abort the pipeline with a clear error message.

### Setup pipeline

| Step | Function | What it does |
|------|----------|--------------|
| 1 | `ensure_system_python` | Searches PATH and well-known locations for Python >= 3.10. If absent, installs one using the platform's best available method (see §3.1). |
| 2 | `find_repo_root` | Walks upward from `__file__` until `pyproject.toml` is found. |
| 3 | `fix_build_backend` | Patches `setuptools.backends.legacy:build` → `setuptools.build_meta` in `pyproject.toml` if present. |
| 4 | `ensure_pipx` | Installs pipx via the platform's package manager with `pip install --user pipx` as universal fallback. Runs `pipx ensurepath`. |
| 5 | `install_scriptoscope` | `pipx install <repo> --force --python <system_python>` |
| 6 | `inject_requirements` | `pipx inject scriptoscope -r requirements.txt --force` |
| 7 | `inject_test_deps` | `pipx inject scriptoscope pytest pytest-asyncio --force` |
| 8 | `install_blast` | Downloads BLAST+ from NCBI FTP, extracts six binaries (see §4). |
| 9 | `run_tests` | `<venv-python> -m pytest tests/ -q` |
| 10 | `build_developer_daemon` | `sh devtools/build-ape.sh` (see §5). |

### 3.1 System Python installation

The platform dispatch covers every supported OS and distro:

| Platform | Primary method | Fallback |
|---|---|---|
| macOS | `brew install python@3.13` (installs Homebrew first if absent) | python.org `.pkg` |
| Ubuntu/Debian | `apt install python3.13` | deadsnakes PPA → pyenv |
| Fedora/RHEL/Rocky | `dnf install python3.13` or module stream | pyenv |
| Arch/Manjaro | `pacman -S python` or AUR via yay/paru | pyenv |
| Alpine | `apk add python3` | edge repos → pyenv |
| openSUSE/SLES | `zypper install python313` | OBS repo → pyenv |
| Gentoo | `emerge dev-lang/python:3.13` | pyenv |
| Void | `xbps-install python3` | pyenv |
| FreeBSD | `pkg install python313` | pyenv |
| OpenBSD | `pkg_add python%3.13` | pyenv |
| NetBSD | `pkgin install python313` | pyenv |
| DragonFlyBSD | `pkg install python313` (dports) | pyenv |
| Windows | winget → Scoop → Chocolatey → python.org `.exe` | — |

pyenv is the universal last resort for Linux and BSD. When invoked, the
bootstrap first installs the complete set of C build dependencies for the
detected distro (gcc, openssl-devel, readline-devel, etc.), then clones/updates
pyenv, builds the requested CPython version from source, and sets it as the
global version.

### 3.2 CLI flags

```
--version X.Y          Target Python version (default: 3.13)
--min-version X.Y      Minimum acceptable existing Python (default: 3.10)
--python-only          Stop after Step 1
--no-tests             Skip Step 9
--no-blast             Skip Step 8
--blast-only           Run Step 8 only and exit
--no-daemon            Skip Step 10
--force-pyenv          Force pyenv regardless of platform
--skip-python-check    Skip the "already installed" check in Step 1
--no-path              Do not modify shell profiles or system PATH
-q, --quiet            Suppress informational output
```

---

## 4. BLAST+ — Why It Is Not Bundled in the APE Binary

### The question

ScriptoScope uses six BLAST+ command-line tools: `blastn`, `blastp`, `blastx`,
`tblastn`, `tblastx`, `makeblastdb`. The natural question was whether they could
be embedded inside `setup-dev-env.com` so the binary truly contains everything.

### Why this is not possible

**Size.** The six required BLAST+ binaries across all five platform/architecture
combinations, compressed:

| Platform | Uncompressed | zstd -19 compressed |
|---|---|---|
| macOS ARM64 | 149 MB | ~41 MB |
| macOS x86_64 | ~175 MB | ~48 MB |
| Linux x86_64 | 197 MB | ~61 MB |
| Linux ARM64 | ~200 MB | ~63 MB |
| Windows x64 | 107 MB | ~33 MB |
| **Total** | **~828 MB** | **~246 MB** |

Pre-bundling all platforms would expand `setup-dev-env.com` from 35 MB to
~281 MB — an 8× size increase — and every user on every platform would download
binaries for operating systems they are not running.

**Dynamic linking.** NCBI's distributed BLAST+ binaries are dynamically linked
against each platform's system runtime:

- macOS: `/usr/lib/libz`, `/usr/lib/libbz2`, `/usr/lib/libSystem`,
  `ApplicationServices.framework`
- Linux: `ld-linux-x86-64.so.2`, `libgcc_s`, `libstdc++`
- Windows: `MSVCRT.dll`

These are different, incompatible ABIs. Making a single BLAST+ binary that runs
on all platforms would require rebuilding the entire NCBI C++ Toolkit from
source against Cosmopolitan Libc — an independent large project that does not
exist and is not maintained by this project.

Cosmopolitan Python works because Cosmopolitan Libc provides a single unified
ABI that all platforms share. BLAST+ does not use Cosmopolitan Libc.

### Decision: download on first use

The bootstrap (Step 8) downloads the correct official NCBI tarball for the
running platform and architecture at setup time, extracts only the six required
binaries, and installs them to `~/.local/blast/bin` (user-local, no root
required). The download is ~200 MB and happens once. The version is validated on
subsequent runs by checking `blastn -version` against `BLAST_VERSION` in the
source; the download is skipped if already current.

```python
BLAST_TARBALLS = {
    ("macos",   "arm64"):   "ncbi-blast-2.17.0+-aarch64-macosx.tar.gz",
    ("macos",   "x86_64"):  "ncbi-blast-2.17.0+-x64-macosx.tar.gz",
    ("linux",   "x86_64"):  "ncbi-blast-2.17.0+-x64-linux.tar.gz",
    ("linux",   "aarch64"): "ncbi-blast-2.17.0+-aarch64-linux.tar.gz",
    ("windows", "x86_64"):  "ncbi-blast-2.17.0+-x64-win64.tar.gz",
    ("freebsd", "x86_64"):  "ncbi-blast-2.17.0+-x64-linux.tar.gz",  # Linux compat
    ("freebsd", "aarch64"): "ncbi-blast-2.17.0+-aarch64-linux.tar.gz",
}
```

FreeBSD uses the Linux x86_64/ARM64 tarball because FreeBSD's Linux
compatibility layer (`linuxulator`) runs Linux ELF binaries directly.

---

## 5. Developer Daemon — `devtools/watch.py` → `developer-daemon.com`

### Motivation

Four manual workflows in `DEVELOPERS.md` required the developer to run specific
commands after saving files:

| File changed | Manual command |
|---|---|
| `scriptoscope.py` | `pipx install . --force` |
| `pyproject.toml` or `requirements.txt` | `pipx install . --force` + `pipx inject -r requirements.txt --force` |
| `devtools/__main__.py` or `devtools/build-ape.sh` | `sh devtools/build-ape.sh` |

The goal was to eliminate this manual loop: save a file, the correct action runs
automatically, the updated application is live.

### Architecture

The daemon has three layers:

**1. File-watching backends** — each platform's most efficient mechanism:

| Platform | Backend | Implementation | Idle CPU |
|---|---|---|---|
| macOS / \*BSD | kqueue | `select.KQ_FILTER_VNODE` on open file descriptors | 0% |
| Linux | inotify | `ctypes.CDLL(libc)` → `inotify_init1` / `inotify_add_watch` | 0% |
| Windows | ReadDirectoryChangesW | `ctypes.WinDLL("kernel32")` | 0% |
| Fallback | stat-polling | `(mtime_ns, size)` comparison every 300 ms | negligible |

kqueue and inotify are event-driven: the watching thread blocks in a kernel
call and is woken only when a filesystem event occurs. This means zero CPU usage
while no files are changing, which matters for a process that runs indefinitely
in a background terminal.

**2. Debouncer** — collects filesystem events within a 500 ms window before
acting. This is necessary because editors typically write files in two steps
(truncate then write, or write to a temp file then rename), which would
otherwise trigger two actions in rapid succession. When multiple watched files
change within the same window, the single highest-priority action is chosen:

```
rebuild APE  >  reinstall + sync deps  >  reinstall app
```

This means if `scriptoscope.py` and `requirements.txt` both change at once,
only "reinstall + sync deps" runs (it is a superset).

**3. Action runner** — a serialised daemon thread that dequeues `(action, repo,
changed_files)` tuples and runs them one at a time, streaming subprocess output
directly to the terminal.

### Key design decision: relative path keys

`WATCHED_FILES` contains repo-relative paths like `"devtools/__main__.py"`.
`ACTION_MAP` is keyed by the same strings. All three OS backends must therefore
call `debouncer.notify(rel_key(repo, path))` — passing the relative path, not
`path.name`. A helper function:

```python
def rel_key(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.name
```

This was a non-trivial fix: the initial implementation passed `path.name` to
the debouncer, which meant `devtools/__main__.py` was notified as `__main__.py`
and never matched `"devtools/__main__.py"` in `ACTION_MAP`.

### Key design decision: re-exec under system Python

The Cosmopolitan Python bundled in `developer-daemon.com` does not have
`_ctypes` (the C extension backing the `ctypes` module) or `select.kqueue` /
`select.epoll`. It has only `select.poll`. This makes the native OS backends
unavailable inside the APE runtime.

The solution: `watch.py` detects this condition at startup and immediately calls
`os.execv` to replace itself with the system Python (installed by
`setup-dev-env.com`), passing the same script and arguments. The system Python
has full `select` and `ctypes` access. This re-exec is completely transparent to
the user.

```python
def _is_cosmo_python() -> bool:
    if sys.executable.endswith(".com"):
        return True
    try:
        import ctypes
        ctypes.CDLL
    except (ImportError, AttributeError):
        return True
    if IS_MACOS and not hasattr(select, "kqueue"):
        return True
    return False
```

---

## 6. Repository Reorganisation — devtools/

### Before

```
ScriptoScope/
├── __main__.py
├── watch.py
├── build-ape.sh
├── setup-dev-env.com
└── watch.com
```

### After

```
ScriptoScope/
├── setup-dev-env.com
├── developer-daemon.com
└── devtools/
    ├── __main__.py
    ├── watch.py
    ├── build-ape.sh
    └── sitecustomize.py
```

The user asked for auxiliary tooling files in a dedicated directory, with only
the two end-user-facing binaries at the repo root.

### Changes required

**`devtools/build-ape.sh`** — The script was previously located at the repo root
and set `SCRIPT_DIR` as the base for all paths. After moving to `devtools/`,
`SCRIPT_DIR` now resolves to `devtools/`. A `REPO_ROOT` variable was added:

```sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="${REPO_ROOT}/setup-dev-env.com"
DAEMON_OUTPUT="${REPO_ROOT}/developer-daemon.com"
```

Both output binaries are placed at the repo root (two levels up from where the
script lives), keeping the developer-facing entry points at the top level.

**`devtools/watch.py`** — `_action_rebuild_ape` was updated to invoke
`repo / "devtools" / "build-ape.sh"` instead of `repo / "build-ape.sh"`. The
`WATCHED_FILES` list was updated to use `devtools/`-relative paths.

**`watch.com` renamed to `developer-daemon.com`** — the name `watch.com` was
considered too generic. `developer-daemon.com` accurately describes its role.

---

## 7. setup-dev-env.com Committed to Git

### The original problem

`setup-dev-env.com` was gitignored. This created a circular dependency: to run
`setup-dev-env.com`, you first had to run `sh devtools/build-ape.sh`, which
requires `sh`, `curl`, and `zip` to be available — all platform-specific
concerns that the binary was supposed to eliminate.

The Quick Start in `DEVELOPERS.md` was:

```sh
git clone https://github.com/Binomica-Labs/ScriptoScope.git
cd ScriptoScope
sh devtools/build-ape.sh    # ← OS-specific, defeats the purpose
./setup-dev-env.com
```

### Decision

`setup-dev-env.com` is now committed to git. At 35 MB it is well within
GitHub's 100 MB per-file limit, and its purpose is specifically to be
distributed — committing it is the correct choice.

`developer-daemon.com` remains gitignored. It is a developer tool, not a user
tool, and it is built automatically by `setup-dev-env.com` as Step 10.

The Quick Start is now:

```sh
git clone https://github.com/Binomica-Labs/ScriptoScope.git
cd ScriptoScope
./setup-dev-env.com
```

### Step 10: building the daemon

`devtools/__main__.py`'s `build_developer_daemon()` function:

1. Checks that `devtools/build-ape.sh` exists.
2. Checks that `curl` (or `wget`) and `zip` are available. These tools exist on
   every supported platform after a standard OS install. If absent, a clear
   warning is emitted and the step is skipped non-fatally.
3. Runs `sh devtools/build-ape.sh`, which builds both APE binaries (the
   running `setup-dev-env.com` is overwritten with a fresh copy of itself, which
   is safe on Unix because the kernel keeps the old inode mapped while the
   process is running).

The `--no-daemon` flag skips this step entirely.

---

## 8. The Bare Invocation Bug — `sitecustomize.py`

### Symptom

Running `./setup-dev-env.com` opened an interactive Python REPL instead of
running the bootstrap. The binary appeared non-functional.

### Root cause investigation

An APE binary **is** a Python interpreter. When the OS executes it natively
(Mach-O on macOS, ELF on Linux, PE on Windows), Python starts with no script
argument. Python's startup semantics in this case: open an interactive REPL
reading from stdin.

Python only executes a ZIP's `__main__.py` when the ZIP is passed as the
*script argument*, i.e.:

```
python-interpreter   zipapp.zip   [user-flags...]
     argv[0]           argv[1]       argv[2+]
```

In the APE case, the binary is both `argv[0]` (the interpreter) and the ZIP
archive. There is no separate `argv[1]` script path, so `__main__.py` is never
reached.

### Why `sh setup-dev-env.com` also fails for flags

The APE shell preamble (read from the binary when invoked as a shell script)
extracts a native Python binary to `~/.ape-1.10` and runs:

```sh
exec "$t" "$o" "$@"
```

Where `$t` is the extracted Python interpreter, `$o` is our APE binary, and
`$@` are the user's arguments. This invocation makes `$o` Python's own name
(its `argv[0]`), not the script path. Any flags in `$@` are therefore
interpreted as Python's own flags, and Python rejects unknown flags like
`--no-tests` with an error before any user code runs.

This was confirmed by inspecting the actual preamble text extracted from the
binary:

```sh
o=$(command -v "$0")
t="${TMPDIR:-${HOME:-.}}/.ape-1.10"
[ -x "$t" ] && exec "$t" "$o" "$@"
# ... (extracts $t from the binary, then:)
exec "$t" "$o" "$@"
```

### Approaches considered and rejected

**Prepending a shell wrapper** — The APE preamble begins at byte 0 with `MZ`
(Windows PE magic). Any content before byte 0 does not exist. The preamble
cannot be modified without breaking the PE/ELF/Mach-O headers.

**Using `ctypes` to read `/proc/self/cmdline`** — `ctypes` is not available in
Cosmopolitan Python (`_ctypes` C extension is absent). `/proc` does not exist
on macOS.

**A separate thin wrapper binary** — Would require distributing two files,
defeating the single-binary goal.

### Solution: `devtools/sitecustomize.py`

Python automatically imports a module named `sitecustomize` from `sys.path`
at startup, before any user script runs, before Python checks whether to start
a REPL. The Cosmopolitan Python's `sys.path` includes `/zip/Lib`, which maps
to `Lib/` inside the binary's ZIP layer.

`devtools/sitecustomize.py` is injected at `Lib/sitecustomize.py` in both
binaries during the build process. Python imports it unconditionally at every
startup.

The module performs one action: when `sys.argv == ['']` (the canonical state
for bare invocation with no script), it calls `os.execv` to replace the current
process with:

```python
os.execv(exe, [exe, exe] + extra_flags)
#              ^^^  ^^^
#              |    |
#              |    Python sees this as the *script* path → runs __main__.py
#              The interpreter path (unchanged)
```

On the second process image, Python sees its own path as the script argument,
treats it as a zipapp, finds `__main__.py`, and executes the bootstrap normally.

### Flag relay via `_APE_ARGS`

Because Python intercepts unknown flags before `sitecustomize` runs:

```
./setup-dev-env.com --no-tests
               ↑
     Python sees this as its own flag
     and rejects it with "unknown option"
     before sitecustomize.py is imported
```

Flags are relayed through the `_APE_ARGS` environment variable instead.
`sitecustomize.py` reads `_APE_ARGS`, splits it on whitespace, and appends the
tokens to the `os.execv` call. The variable is removed from `os.environ` before
the re-exec so `__main__.py` never sees it:

```sh
_APE_ARGS="--no-tests --no-blast" ./setup-dev-env.com
_APE_ARGS="--python-only"         ./setup-dev-env.com
_APE_ARGS="--version 3.12"        ./setup-dev-env.com
```

For users who prefer not to use the environment variable, the self-passing
invocation also works and bypasses the shim entirely:

```sh
./setup-dev-env.com ./setup-dev-env.com --no-tests --no-blast
```

### Safety properties of the shim

The shim guards against misuse with several checks:

1. **Only fires when `sys.argv == ['']`** — any other argv (script path,
   `-c`, `-m`, etc.) means Python was already given a task. The shim does
   nothing.
2. **Verifies `__main__.py` exists in the binary's ZIP before re-execing** —
   prevents the shim from firing in plain `python.com` invocations that happen
   to have no arguments (e.g. `python.com` opened interactively after a `cp
   python.com somewhere`).
3. **Removes `_APE_ARGS` from the environment before re-execing** — bootstrap
   code never sees the relay variable.
4. **Wraps `os.execv` in try/except OSError** — if exec fails for any reason
   (e.g. binary on a `noexec` mount), Python falls through to a REPL rather
   than crashing with an opaque traceback.
5. **Deletes all names it introduces** — the global namespace seen by
   `__main__.py` is completely clean, as if `sitecustomize.py` never existed.

### Build integration

`devtools/build-ape.sh`'s `_assemble_ape` function now injects two files into
each binary:

```sh
# 1. The entry point
zip -j "$OUTPUT" "$MAIN_PY"          # → __main__.py at ZIP root

# 2. The self-invocation shim
mkdir -p "$tmp/Lib"
cp "$SITECUSTOMIZE_PY" "$tmp/Lib/sitecustomize.py"
(cd "$tmp" && zip -r "$OUTPUT" "Lib/sitecustomize.py")
```

`devtools/sitecustomize.py` is also added to `WATCHED_FILES` in `watch.py` and
to `ACTION_MAP` with the `"rebuild APE"` action, so changes to it automatically
trigger a rebuild of both binaries.

---

## 9. Final File Inventory

```
devtools/
├── __main__.py         Bootstrap source for setup-dev-env.com (2040 lines)
│                       Steps 1-10: Python install, pipx, ScriptoScope,
│                       dependencies, BLAST+, tests, daemon build.
│
├── watch.py            Daemon source for developer-daemon.com (1283 lines)
│                       kqueue / inotify / ReadDirectoryChangesW / poll
│                       backends; debouncer; action runner; re-exec shim.
│
├── sitecustomize.py    Self-invocation shim (137 lines)
│                       Injected as Lib/sitecustomize.py into both binaries.
│                       Fixes bare ./setup-dev-env.com invocation.
│
├── build-ape.sh        Build script (560+ lines, POSIX sh)
│                       Downloads python.com, assembles both APE binaries,
│                       verifies, smoke-tests.
│
└── DEVTOOLDEVELOPMENTLOG.md   This file.
```

Output binaries (at repo root, built by `devtools/build-ape.sh`):

```
setup-dev-env.com       35 MB APE — committed to git
                        Contains: Cosmopolitan Python 3.12 + __main__.py
                                  + Lib/sitecustomize.py

developer-daemon.com    35 MB APE — gitignored, built on first run
                        Contains: Cosmopolitan Python 3.12 + watch.py (as __main__.py)
                                  + Lib/sitecustomize.py
```

---

## 10. Known Limitations and Future Work

**Flag passing UX.** The `_APE_ARGS` environment variable works but is
non-obvious. A better long-term solution would be to upstream a change to
Cosmopolitan Python so that bare invocation of an APE binary that contains a
`__main__.py` automatically runs it. The `sitecustomize.py` shim is an
acceptable workaround until that exists.

**Windows direct execution.** On Windows, `setup-dev-env.com` must be renamed
to `setup-dev-env.exe` before the OS will execute it natively. The `.com`
extension is associated with the 16-bit DOS command format, not with APE
binaries. There is no technical workaround short of distributing a separate
`.exe` file or a PowerShell bootstrap script that performs the rename.

**`developer-daemon.com` and ARM64 Linux inotify.** The inotify backend relies
on `ctypes.CDLL(libc)`. If the system Python was built without `_ctypes` (rare
but possible on some Alpine musl builds), the daemon falls back to
stat-polling. This is transparent but less efficient. The polling interval is
configurable via `--interval`.

**BLAST+ version pinning.** `BLAST_VERSION = "2.17.0+"` is hardcoded in
`devtools/__main__.py`. When NCBI releases a new BLAST+ version, this constant
must be updated manually, the binary rebuilt, and the commit pushed. There is
no automatic update check.

**`developer-daemon.com` self-rebuild loop.** When `devtools/watch.py` changes,
the daemon triggers `sh devtools/build-ape.sh`, which writes a new
`developer-daemon.com` to disk. The running daemon process is not automatically
restarted; the developer must restart it manually after the build completes.
This is by design — automatically restarting a process that is monitoring itself
for changes requires careful handling of the running process state that is not
worth the complexity.

---

## 11. Pure-Python Daemon Builder — Eliminating Shell Dependencies

### Symptom

Running `./setup-dev-env.com` on a system where `sh`, `curl`, `wget`, or `zip`
were unavailable (or where the shell build script failed for any reason) produced:

```
[WARN]  devtools/build-ape.sh exited with code 1.
  developer-daemon.com may not have been built.
  Run manually:  sh devtools/build-ape.sh
```

This told the user to run a platform-specific shell script, which is exactly the
class of problem the entire devtools system was designed to eliminate.

### Root cause

`build_developer_daemon()` in `devtools/__main__.py` delegated everything to
`sh devtools/build-ape.sh`. That shell script required four external tools:

| Tool | Purpose | Platform concern |
|---|---|---|
| `sh` | Run the build script itself | Not available on stock Windows without WSL |
| `curl` or `wget` | Download `python.com` from cosmo.zip | May not be installed on minimal distros |
| `zip` | Inject `__main__.py` and `sitecustomize.py` into the binary | Not installed by default on many systems |

Every one of these is a platform-specific dependency. The bootstrap binary
exists precisely to avoid requiring platform-specific tools, so delegating to a
shell script that requires them was architecturally wrong.

### Solution: pure stdlib reimplementation

`build_developer_daemon()` was rewritten to perform the entire build using only
Python standard library modules — all of which are available inside the bundled
Cosmopolitan Python:

| Operation | Old (shell) | New (pure Python) |
|---|---|---|
| Download `python.com` | `curl -fsSL` / `wget -O` | `urllib.request.urlretrieve()` |
| Copy binary to output path | `cp` | `shutil.copy2()` |
| Inject files into ZIP layer | `zip -j` / `zip -r` | `zipfile.ZipFile(mode='a')` |
| Verify ZIP contents | `unzip -l \| grep` | `zipfile.ZipFile.namelist()` |

The implementation tries external download tools first when available (for
better progress display), but the stdlib fallback is always present:

```python
def _download_cosmo_python(dest: Path) -> bool:
    # Try curl first (best terminal progress)
    if have("curl"):
        try:
            run(["curl", "-fL", "--progress-bar", "-o", str(dest), url])
            return True
        except: pass

    # Try wget
    if have("wget"):
        try:
            run(["wget", "-O", str(dest), url])
            return True
        except: pass

    # Pure-stdlib fallback — always works, no external tools
    urllib.request.urlretrieve(url, str(dest))
    return True
```

The ZIP injection uses Python's `zipfile` module in append mode, which writes
new entries to the end of an existing ZIP archive without disturbing the
binary's ELF/Mach-O/PE headers:

```python
with zipfile.ZipFile(str(output), "a", compression=zipfile.ZIP_DEFLATED) as z:
    z.write(str(watch_py), "__main__.py")
    z.write(str(sitecustomize_py), "Lib/sitecustomize.py")
```

This is functionally identical to what `build-ape.sh` does with the `zip`
command, but requires zero external tools.

### Caching

The Cosmopolitan Python binary (~35 MB) is cached at
`~/.cache/scriptoscope-ape/python.com` (or `$XDG_CACHE_HOME/scriptoscope-ape/`
if set, or `%LOCALAPPDATA%/scriptoscope-ape/` on Windows). The cache is shared
with `devtools/build-ape.sh` — both use the same directory and filename.
Subsequent builds (after the first download) are nearly instant because only the
copy + zip injection steps run.

### What `devtools/build-ape.sh` is still for

`devtools/build-ape.sh` is no longer invoked by `setup-dev-env.com`. It remains
in the repository for two use cases:

1. **Rebuilding `setup-dev-env.com` itself** — the committed binary. This is
   only needed when `devtools/__main__.py` or `devtools/sitecustomize.py`
   changes. Developers editing these files run `sh devtools/build-ape.sh`
   manually (or let `developer-daemon.com` do it automatically via the file
   watcher).

2. **Rebuilding both binaries at once** — useful when editing `devtools/watch.py`
   and `devtools/__main__.py` simultaneously, or after a Cosmopolitan Python
   update.

For normal users who just clone the repo and run `./setup-dev-env.com`, the
shell script is never invoked. The entire pipeline — from bare invocation
through to a working `developer-daemon.com` — runs without `sh`, `curl`, `wget`,
or `zip`.