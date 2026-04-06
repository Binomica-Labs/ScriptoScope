# ScriptoScope — Developer Guide

> **ScriptoScope v0.6.0** — TUI Transcriptome Browser  
> Built with [Textual](https://github.com/Textualize/textual), [Rich](https://github.com/Textualize/rich), [Biopython](https://biopython.org/), and [pyhmmer](https://pyhmmer.readthedocs.io/)  
> BLAST+ is downloaded automatically during setup — no manual installation needed.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start — One Binary, Every OS](#quick-start)
3. [How the Bootstrap Binary Works](#how-the-bootstrap-binary-works)
4. [Building the APE Binaries](#building-the-ape-binaries)
5. [What the Bootstrap Does](#what-the-bootstrap-does)
6. [The Dev Watcher — developer-daemon.com](#the-dev-watcher)
7. [Manual Setup (step-by-step)](#manual-setup)
8. [Project Structure](#project-structure)
9. [Running Tests](#running-tests)
10. [Development Workflow](#development-workflow)
11. [Environment Variables](#environment-variables)
12. [Troubleshooting](#troubleshooting)
13. [Contributing](#contributing)

---

## Overview

| Property        | Value                            |
|-----------------|----------------------------------|
| Version         | 0.6.0                            |
| Python required | >= 3.10                          |
| Entry point     | `scriptoscope`                   |
| Build backend   | `setuptools.build_meta`          |
| Test runner     | pytest + pytest-asyncio          |
| Log file        | `/tmp/scriptoscope.log`          |

### Dependencies

| Package     | Minimum | Purpose                           |
|-------------|---------|-----------------------------------|
| `textual`   | 8.2.2   | TUI framework                     |
| `rich`      | 14.3.3  | Terminal rendering                |
| `biopython` | 1.87    | Sequence parsing, NCBI BLAST      |
| `pyhmmer`   | 0.12.0  | HMM profile searching (Pfam)      |

**BLAST+** (`blastn`, `blastp`, `blastx`, `tblastn`, `tblastx`, `makeblastdb`) is downloaded automatically during setup from NCBI FTP and installed to `~/.local/blast/bin`. No manual installation or root access required. Pass `--no-blast` to skip.

---

## Quick Start

The fastest way to get a working developer environment on **any OS** is the bootstrap binary:

```sh
# Clone the repository
git clone https://github.com/Binomica-Labs/ScriptoScope.git
cd ScriptoScope

# Run the bootstrap — that's it
./setup-dev-env.com
```

`setup-dev-env.com` is **committed to git**, so cloning the repo gives you a working binary immediately — no build step, no shell commands, no prerequisites. It carries its own Python interpreter (Cosmopolitan Python 3.12) and runs natively on macOS, Linux, Windows, and BSD.

> **How bare invocation works:** APE binaries are simultaneously a native executable *and* a Python interpreter. When you run `./setup-dev-env.com` without arguments, a `sitecustomize.py` embedded in the binary detects this and uses `os.execv` to restart the process, passing the binary to itself as the script argument. Python then treats it as a zipapp and executes `__main__.py`. This is completely transparent.

It handles everything in a single run:
- Installs system Python if needed
- Installs pipx
- Installs ScriptoScope and all dependencies
- Downloads and installs BLAST+
- Runs the test suite
- Builds `developer-daemon.com` for ongoing development

After it finishes, start the daemon in a second terminal and leave it running while you develop:

```sh
./developer-daemon.com
```

From that point on, every file save triggers the correct action automatically — no manual `pipx install`, no forgotten rebuilds.

### Platform notes

| Platform | How to run |
|---|---|
| **macOS** | `./setup-dev-env.com` |
| **Linux** | `./setup-dev-env.com` — or `sh setup-dev-env.com` if direct execution fails |
| **Windows** | `Rename-Item setup-dev-env.com setup-dev-env.exe` then `.\setup-dev-env.exe` |
| **FreeBSD / OpenBSD / NetBSD** | `./setup-dev-env.com` — or `sh setup-dev-env.com` |


### All flags

**`setup-dev-env.com`**

```
  --version X.Y          System Python to install (default: 3.13)
  --min-version X.Y      Minimum acceptable system Python (default: 3.10)
  --python-only          Install system Python only; stop before pipx/ScriptoScope
  --no-tests             Skip the test suite after setup
  --no-blast             Skip downloading and installing BLAST+
  --blast-only           Only download and install BLAST+; skip everything else
  --no-daemon            Skip building developer-daemon.com
  --force-pyenv          Force pyenv for Python installation (any platform)
  --skip-python-check    Reinstall Python even if a suitable version exists
  --no-path              Do not modify shell profiles or PATH
  -q, --quiet            Suppress informational output
  -h, --help             Show help and exit
```

> **Passing flags:** Because `setup-dev-env.com` is also a Python interpreter, Python intercepts unknown flags (like `--no-tests`) before the bootstrap code runs. Use the `_APE_ARGS` environment variable to relay flags:
>
> ```sh
> _APE_ARGS="--no-tests --no-blast" ./setup-dev-env.com
> _APE_ARGS="--python-only" ./setup-dev-env.com
> _APE_ARGS="--version 3.12" ./setup-dev-env.com
> ```
>
> Alternatively, pass the binary as its own first argument — this bypasses the shim entirely and supports all flags directly:
>
> ```sh
> ./setup-dev-env.com ./setup-dev-env.com --no-tests --no-blast
> ```

**`developer-daemon.com`**

```
  REPO_PATH              Path to repo root (default: auto-detected from script location)
  --poll                 Force stat-polling even when a native backend is available
  --interval SECONDS     Polling interval (default: 0.3; only used with --poll)
  --no-color             Disable ANSI colour output
  -q, --quiet            Suppress informational output
  -h, --help             Show help and exit
```

---

## How the Bootstrap Binary Works {#how-the-bootstrap-binary-works}

`setup-dev-env.com` is an [Actually Portable Executable (APE)](https://justine.lol/ape.html) built with [Cosmopolitan Libc](https://github.com/jart/cosmopolitan). A single file is simultaneously:

- A valid **Windows PE** executable (the `MZ` header at byte 0)
- A valid **Linux / FreeBSD / NetBSD / DragonFlyBSD ELF** binary
- A valid **macOS Mach-O** fat binary (x86_64 + ARM64)
- A valid **OpenBSD ELF** binary
- A valid **POSIX shell script** (the APE header is legal `sh` syntax — so `sh setup-dev-env.com` also works)
- A valid **ZIP archive** (PKZIP appends its central directory at the end of the file)

The ZIP layer contains a full CPython 3.12 standard library and a `__main__.py` at the archive root. When the binary runs itself as a Python zipapp, Python's `zipimport` machinery finds `__main__.py` and executes it — that is the bootstrap logic, written in pure stdlib Python.

```
setup-dev-env.com  (35 MB)
│
├── [bytes 0-1]    "MZ"      → Windows PE loader picks it up as an EXE
├── [bytes 0-5]    "MZqFpD"  → valid x86 JMP sequence; POSIX shells read past it
├── ELF segment              → Linux / BSD kernel maps and runs this
├── Mach-O segment           → macOS kernel maps and runs this (x86_64 + ARM64)
│
└── ZIP archive
    ├── Lib/                     full CPython 3.12 stdlib (batteries included)
    ├── Lib/sitecustomize.py     ← auto-imported at Python startup; detects bare
    │                               invocation (./setup-dev-env.com with no args)
    │                               and re-execs the binary as its own zipapp
    └── __main__.py              ← the bootstrap logic (platform detection,
                                     Python install, pipx install, ScriptoScope
                                     install, dependency injection, test run)
```

Because CPython is **bundled inside the binary**, `setup-dev-env.com` can run before any Python is present on the target machine — no chicken-and-egg problem.

### Linux: direct execution

Some Linux configurations use `binfmt_misc` to route MZ-magic files to WINE. If you see `run-detectors: unable to find an interpreter`, either:

```sh
# Option A — always works, no setup:
sh setup-dev-env.com

# Option B — register the APE loader once; after this ./setup-dev-env.com works forever:
sudo wget -O /usr/bin/ape https://cosmo.zip/pub/cosmos/bin/ape-$(uname -m).elf
sudo chmod +x /usr/bin/ape
sudo sh -c "echo ':APE:M::MZqFpD::/usr/bin/ape:' >/proc/sys/fs/binfmt_misc/register"
```

### WSL

Disable the Windows-binary interceptor before running:

```sh
sudo sh -c "echo -1 > /proc/sys/fs/binfmt_misc/WSLInterop"
./setup-dev-env.com
```

---

## Building the APE Binaries {#building-the-ape-binaries}

**`setup-dev-env.com` is committed to git.** Clone the repo and it is immediately ready to run — no build step required.

**`developer-daemon.com` is built automatically** by `setup-dev-env.com` as its final step (Step 11), using pure Python stdlib — no `sh`, `curl`, `wget`, or `zip` binary required. The Cosmopolitan Python base binary is downloaded via `urllib.request` (or `curl`/`wget` if available for better progress display), and file injection uses Python's `zipfile` module. Pass `--no-daemon` to skip this step.

### How Step 11 builds the daemon (no external tools needed)

The daemon build is implemented entirely in Python stdlib inside `devtools/__main__.py`:

1. **Download `python.com`** — tries `curl` first (for progress bar), then `wget`, then falls back to `urllib.request.urlretrieve()` which is always available inside the APE binary. No external tools are required.
2. **Cache** — the ~35 MB download is cached at `~/.cache/scriptoscope-ape/python.com`. Subsequent runs skip the download entirely.
3. **Copy** — `shutil.copy2()` copies the cached binary to `./developer-daemon.com`.
4. **Inject** — `zipfile.ZipFile(mode='a')` appends `devtools/watch.py` as `__main__.py` and `devtools/sitecustomize.py` as `Lib/sitecustomize.py` into the binary's ZIP layer.
5. **Verify** — `zipfile.ZipFile.namelist()` confirms both entries are present.

The `sitecustomize.py` layer is what makes `./developer-daemon.com` run correctly when invoked bare with no arguments (see [How the Bootstrap Binary Works](#how-the-bootstrap-binary-works)).

### Rebuilding `setup-dev-env.com` (developer-only)

`setup-dev-env.com` is committed to git and rarely needs rebuilding. When you edit files in `devtools/` that affect the bootstrap binary, use the shell build script:

```sh
sh devtools/build-ape.sh
```

This script requires `curl` (or `wget`) and `zip`, and is the only part of the devtools that depends on platform-specific tools. Normal users never need to run it — it exists for developers who are modifying the bootstrap itself.

### `devtools/build-ape.sh` flags

```
sh devtools/build-ape.sh [OPTIONS]

  --output PATH           Output path for setup-dev-env.com (default: <repo-root>/setup-dev-env.com)
  --daemon-output PATH    Output path for developer-daemon.com (default: <repo-root>/developer-daemon.com)
  --python-url URL        Cosmopolitan Python URL (default: https://cosmo.zip/pub/cosmos/bin/python)
  --force                 Re-download python.com even if cached
  --no-cache              Do not cache the download (implies --force)
  --no-daemon             Skip building developer-daemon.com
  -h, --help              Show help and exit
```

---

## What the Bootstrap Does

Running `./setup-dev-env.com` executes the following steps in order:

| Step | What happens |
|---|---|
| **1** | Detect OS, architecture, and Linux distro |
| **2** | Search for a system Python >= 3.10 already on PATH or in well-known locations |
| **3** | If none found: install Python 3.13 using the best available method for the platform (see table below), falling back to [pyenv](https://github.com/pyenv/pyenv) if the native package manager doesn't have the right version |
| **4** | Find the `pyproject.toml` in the repo and fix the build backend if needed |
| **5** | Install pipx using the native package manager, falling back to `pip install --user pipx` |
| **6** | `pipx install . --python <system-python>` — installs ScriptoScope into an isolated venv |
| **7** | `pipx inject scriptoscope -r requirements.txt` — syncs pinned dependency versions |
| **8** | `pipx inject scriptoscope pytest pytest-asyncio` — adds test tooling to the venv |
| **9** | Download BLAST+ 2.17.0+ for the current platform/arch from NCBI FTP, extract the 6 required binaries, install to `~/.local/blast/bin`, add to PATH |
| **10** | Runs `pytest tests/ -q` via the venv's Python |
| **11** | Builds `developer-daemon.com` using pure Python stdlib (`urllib.request` + `zipfile` — no `sh`, `curl`, `wget`, or `zip` binary required; skippable with `--no-daemon`) |
| **12** | Prints a summary with activation, BLAST+ location, and daemon path |



### BLAST+ installation by platform

The bootstrap downloads the correct official NCBI tarball for the running platform and architecture, extracts only the 6 binaries ScriptoScope needs, and installs them to `~/.local/blast/bin` — no root access required, nothing written to system directories.

| Platform | Arch | NCBI tarball |
|---|---|---|
| macOS | ARM64 (Apple Silicon) | `ncbi-blast-2.17.0+-aarch64-macosx.tar.gz` |
| macOS | x86_64 (Intel) | `ncbi-blast-2.17.0+-x64-macosx.tar.gz` |
| Linux | x86_64 | `ncbi-blast-2.17.0+-x64-linux.tar.gz` |
| Linux | ARM64 | `ncbi-blast-2.17.0+-aarch64-linux.tar.gz` |
| Windows | x86_64 | `ncbi-blast-2.17.0+-x64-win64.tar.gz` |
| FreeBSD | x86_64 | `ncbi-blast-2.17.0+-x64-linux.tar.gz` (via Linux compat) |
| FreeBSD | ARM64 | `ncbi-blast-2.17.0+-aarch64-linux.tar.gz` (via Linux compat) |

**Why not pre-bundle BLAST+ inside the APE binary?** Each BLAST+ binary is ~26–34 MB uncompressed and dynamically linked against its platform's system libraries (libSystem on macOS, glibc on Linux, MSVCRT on Windows). Pre-bundling all platform variants would add ~246 MB of compressed payload to the binary — a ~8× size increase — and every user would download all platforms regardless of which one they're on. Downloading on first use is the right call: ~200 MB once, only the right platform, cached by the OS's download tooling.

### Python installation methods by platform

| Platform | Primary method | Fallback |
|---|---|---|
| macOS | `brew install python@3.13` (installs Homebrew first if missing) | python.org `.pkg` installer |
| Ubuntu / Debian / Mint | `apt install python3.13` | deadsnakes PPA → pyenv |
| Fedora / RHEL / Rocky / Alma | `dnf install python3.13` or module stream | pyenv |
| Arch / Manjaro | `pacman -S python` or AUR via `yay`/`paru` | pyenv |
| Alpine | `apk add python3` | edge repos → pyenv |
| openSUSE / SLES | `zypper install python313` | OBS devel:languages:python → pyenv |
| Gentoo | `emerge dev-lang/python:3.13` | pyenv |
| Void Linux | `xbps-install python3` | pyenv |
| FreeBSD | `pkg install python313` | pyenv |
| OpenBSD | `pkg_add python%3.13` | pyenv |
| NetBSD | `pkgin install python313` | pyenv |
| DragonFlyBSD | `pkg install python313` (dports) | pyenv |
| Windows | winget → Scoop → Chocolatey → python.org `.exe` | — |

### BLAST+-only install

You can install or re-install BLAST+ independently at any time without re-running the full setup:

```sh
./setup-dev-env.com --blast-only
```

To skip BLAST+ entirely (e.g. you only use NCBI remote BLAST, not local searches):

```sh
./setup-dev-env.com --no-blast
```

---

## The Dev Watcher {#the-dev-watcher}

`developer-daemon.com` is the file-watcher daemon for iterative development. Run it in a dedicated terminal after `setup-dev-env.com` has finished, and leave it running while you code. Every time you save a watched file, the daemon detects the change and automatically executes the correct rebuild or reinstall action — exactly as if you had typed the command yourself.

### Watched files and actions

| File changed | Action taken | Why |
|---|---|---|
| `scriptoscope.py` | `pipx install . --force` | Reinstalls the app from the local clone so the new code is immediately live in the `scriptoscope` command |
| `pyproject.toml` | `pipx install . --force` then `pipx inject scriptoscope -r requirements.txt --force` | Metadata or dependency spec changed — reinstall and re-sync all deps |
| `requirements.txt` | `pipx install . --force` then `pipx inject scriptoscope -r requirements.txt --force` | Pinned versions changed — reinstall and re-sync all deps |
| `devtools/__main__.py` | `sh devtools/build-ape.sh` | Bootstrap source changed — rebuild both APE binaries |
| `devtools/watch.py` | `sh devtools/build-ape.sh` | Daemon source changed — rebuild both APE binaries |
| `devtools/build-ape.sh` | `sh devtools/build-ape.sh` | Build script itself changed — rebuild both APE binaries |
| `devtools/sitecustomize.py` | `sh devtools/build-ape.sh` | Self-invocation shim changed — rebuild both APE binaries |

When multiple files change within the debounce window (default: 500 ms), the watcher runs the **single highest-priority action** that covers all of them:

```
rebuild APE  >  reinstall + sync deps  >  reinstall app
```

For example, if you save both `scriptoscope.py` and `requirements.txt` in rapid succession, only `reinstall + sync deps` runs (it is a superset of `reinstall app`).

### File-watching backends

The watcher automatically selects the most efficient backend for the current platform:

| Platform | Backend | CPU usage when idle |
|---|---|---|
| macOS / FreeBSD / OpenBSD / NetBSD | `kqueue` (kernel event queue) | 0% |
| Linux | `inotify` (kernel filesystem events) | 0% |
| Windows | `ReadDirectoryChangesW` (Win32 API) | 0% |
| Any (fallback / `--poll`) | stat-polling every 300 ms | negligible |

The Cosmopolitan Python bundled inside `developer-daemon.com` lacks native `kqueue`/`inotify` bindings, so the daemon automatically re-execs itself under the system Python (installed by `setup-dev-env.com`) to gain access to these APIs. This is invisible to the user.

### Starting the watcher

```sh
# In a second terminal, from the repo root:
./developer-daemon.com

# Or if direct execution fails on some Linux configs:
sh developer-daemon.com

# Force polling (useful for NFS mounts or unusual filesystems):
./developer-daemon.com --poll

# Adjust the polling interval:
./developer-daemon.com --poll --interval 0.5

# Watch a repo in a non-standard location:
./developer-daemon.com /path/to/ScriptoScope

# Windows (rename first):
Rename-Item developer-daemon.com developer-daemon.exe
.\developer-daemon.exe
```

### Example session

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ScriptoScope Developer Daemon  v1.0.0
  Ctrl-C to stop
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Repo     : /home/user/ScriptoScope
  Backend  : kqueue
  Debounce : 0.5s

  Watching:
    scriptoscope.py          → reinstall app
    pyproject.toml           → reinstall + sync deps
    requirements.txt         → reinstall + sync deps
    devtools/__main__.py     → rebuild APE
    devtools/watch.py        → rebuild APE
    devtools/build-ape.sh    → rebuild APE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

14:03:17 [watch] Developer daemon active. Watching 6 files in /home/user/ScriptoScope

# (you save scriptoscope.py)

───────────────────────────────────────────────────────────────
  scriptoscope.py changed → reinstalling via pipx
───────────────────────────────────────────────────────────────
14:03:22 [watch] Changed: scriptoscope.py
14:03:22 [ run ] pipx install /home/user/ScriptoScope --force
  installed package scriptoscope 0.6.0, installed using Python 3.13.3 ✨
14:03:28 [  ok ] reinstall app completed in 5.9s — watching for more changes…
```

### Stopping the watcher

Press **Ctrl-C** in the terminal where `developer-daemon.com` is running. It shuts down cleanly.

---

## Manual Setup {#manual-setup}

If you prefer full control over each step, or can't run the bootstrap binary, follow these steps.

### 1. Install Python 3.10+

You need Python 3.10 or newer on your system. The bootstrap binary does this automatically, but manually:

**macOS:**
```sh
brew install python@3.13
```

**Ubuntu 23.04+ / Debian 12+:**
```sh
sudo apt update && sudo apt install python3.13 python3.13-venv
```

**Fedora:**
```sh
sudo dnf install python3.13
```

**Windows:**
```powershell
winget install Python.Python.3.13
# or download from https://www.python.org/downloads/windows/
```

**FreeBSD:**
```sh
sudo pkg install python313
```

**Any platform (pyenv):**
```sh
curl https://pyenv.run | sh
pyenv install 3.13
pyenv global 3.13
```

### 2. Install pipx

```sh
# macOS
brew install pipx && pipx ensurepath

# Ubuntu 23.04+ / Debian 12+
sudo apt install pipx && pipx ensurepath

# Fedora
sudo dnf install pipx && pipx ensurepath

# Any platform (pip fallback)
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

After running `pipx ensurepath`, restart your shell (or `source ~/.bashrc` / `source ~/.zshrc`) to pick up the updated PATH.

### 3. Clone the repository

```sh
git clone https://github.com/Binomica-Labs/ScriptoScope.git
cd ScriptoScope
```

### 4. Fix the build backend (if upstream hasn't already)

The `pyproject.toml` must use `setuptools.build_meta`. Check the `[build-system]` section:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"   # ← must be this
```

If it reads `setuptools.backends.legacy:build` instead, fix it:

```sh
sed -i.bak 's|setuptools.backends.legacy:build|setuptools.build_meta|' pyproject.toml
```

The bootstrap binary (`setup-dev-env.com`) patches this automatically.

### 5. Install BLAST+

The bootstrap handles this automatically. To do it manually, download the tarball for your platform from:

```
https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST/
```

Extract the 6 binaries ScriptoScope uses (`blastn`, `blastp`, `blastx`, `tblastn`, `tblastx`, `makeblastdb`) somewhere on your `PATH`, e.g. `~/.local/blast/bin`. No root required.

Or use the bootstrap's `--blast-only` flag:

```sh
./setup-dev-env.com --blast-only
```

### 6. Install ScriptoScope via pipx

```sh
# From inside the cloned directory:
pipx install . --python python3.13
```

All four dependencies (`textual`, `rich`, `biopython`, `pyhmmer`) are installed automatically from `pyproject.toml`.

### 7. Sync requirements.txt

```sh
pipx inject scriptoscope -r requirements.txt
```

### 8. Verify

```sh
pipx list                      # should show scriptoscope 0.6.0
pipx runpip scriptoscope list  # shows all installed packages in the venv
scriptoscope --help            # or just: scriptoscope
blastn -version                # confirm BLAST+ is on PATH
```

---

## Project Structure {#project-structure}

```
ScriptoScope/
├── scriptoscope.py           Main application (single-file TUI)
├── pyproject.toml            Package metadata and build configuration
├── requirements.txt          Pinned dependency versions
├── setup-dev-env.com         ← committed to git (35 MB APE binary); run after cloning
├── developer-daemon.com      ← built by setup-dev-env.com on first run, gitignored
├── devtools/
│   ├── __main__.py           Bootstrap source — zipped into setup-dev-env.com
│   │                         (Python install, pipx, ScriptoScope, BLAST+ download,
│   │                          tests, builds developer-daemon.com as final step)
│   ├── watch.py              Daemon source — zipped into developer-daemon.com
│   │                         (kqueue/inotify/RDCW/polling backends, action runner, debouncer)
│   ├── sitecustomize.py      Auto-run shim — zipped into both binaries as Lib/sitecustomize.py
│   │                         (Python imports this at startup; re-execs bare ./binary invocations
│   │                          as zipapps; relays flags from the _APE_ARGS env var)
│   └── build-ape.sh          Developer-only: rebuilds both APE binaries (needs curl + zip)
├── tests/
│   ├── conftest.py
│   ├── test_smoke.py         61 end-to-end UI/behaviour tests
│   └── test_dna_sanity.py    44 data-integrity tests (codons, ORFs, FASTA parsing)
├── DEVELOPERS.md             This file
└── README.md                 User-facing documentation
```

### The APE build pipeline

```
devtools/__main__.py ────┐
devtools/sitecustomize.py ├── zip + python.com ──→ setup-dev-env.com     (35 MB, committed to git)
                          │
devtools/watch.py ───────┐│
devtools/sitecustomize.py ┘└─────────────────────→ developer-daemon.com  (35 MB, built on first run)

python.com = Cosmopolitan Python 3.12, downloaded once and cached by devtools/build-ape.sh
Both outputs placed at repo root, run natively on macOS / Linux / Windows / BSD (x86_64 + ARM64)
```

---

## Running Tests

The test suite requires `pytest` and `pytest-asyncio`. If you used the bootstrap binary, they were already injected. To add them manually:

```sh
pipx inject scriptoscope pytest pytest-asyncio
```

### Running the tests

Always invoke pytest via the venv's Python so it runs in the correct environment:

```sh
# Linux / macOS
~/.local/pipx/venvs/scriptoscope/bin/python -m pytest tests/ -q

# Windows (PowerShell)
& "$env:USERPROFILE\.local\pipx\venvs\scriptoscope\Scripts\python.exe" -m pytest tests/ -q
```

Or activate the venv first:

```sh
# Linux / macOS
source ~/.local/pipx/venvs/scriptoscope/bin/activate
pytest tests/ -q
deactivate

# Windows (PowerShell)
& "$env:USERPROFILE\.local\pipx\venvs\scriptoscope\Scripts\Activate.ps1"
pytest tests/ -q
deactivate
```

### Test suite

| File | Tests | What it covers |
|---|---|---|
| `tests/test_smoke.py` | 61 | End-to-end UI behaviour: FASTA loading, filters, stats, ORF finding, BLAST/HMMER panels, widget wiring |
| `tests/test_dna_sanity.py` | 44 | **Sacred territory** — codon table correctness, regex exhaustiveness, reverse complement, hand-crafted ORF ground truth, Biopython cross-validation, byte-exact FASTA parsing, length arithmetic |
| **Total** | **105** | |

### pytest configuration

`pyproject.toml` sets `asyncio_mode = "auto"`, so all `async def test_*` functions are treated as asyncio tests automatically. This is why `pytest-asyncio` must be present alongside `pytest`.

### Useful test invocations

```sh
VENV=~/.local/pipx/venvs/scriptoscope/bin/python

$VENV -m pytest tests/ -q                    # full suite, quiet
$VENV -m pytest tests/ -v                    # full suite, verbose
$VENV -m pytest tests/test_dna_sanity.py -q  # data-integrity tests only
$VENV -m pytest tests/test_smoke.py -q       # UI smoke tests only
$VENV -m pytest tests/ -q -k "orf"           # tests matching keyword
$VENV -m pytest tests/ -q --tb=short         # short tracebacks on failure
```

---

## Development Workflow {#development-workflow}

### First-time setup

```sh
git clone https://github.com/Binomica-Labs/ScriptoScope.git
cd ScriptoScope

# Run setup — installs Python, pipx, ScriptoScope, BLAST+, tests,
# and builds developer-daemon.com automatically
./setup-dev-env.com

# Start the daemon in a second terminal and leave it running
./developer-daemon.com
```

From here, all four manual workflows from DEVELOPERS.md are handled automatically by the watcher whenever you save a file:

| You edit… | Daemon runs… |
|---|---|
| `scriptoscope.py` | `pipx install . --force` |
| `pyproject.toml` or `requirements.txt` | `pipx install . --force` + `pipx inject scriptoscope -r requirements.txt --force` |
| `devtools/__main__.py`, `devtools/watch.py`, or `devtools/build-ape.sh` | `sh devtools/build-ape.sh` |

### Manual commands (if not using the watcher)

If you prefer to trigger actions yourself:

```sh
# After editing scriptoscope.py:
pipx install . --force

# After editing pyproject.toml or requirements.txt:
pipx install . --force
pipx inject scriptoscope -r requirements.txt --force

# After editing devtools/__main__.py, devtools/watch.py, or devtools/build-ape.sh:
sh devtools/build-ape.sh

# After an NCBI BLAST+ release (update BLAST_VERSION in devtools/__main__.py first):
./setup-dev-env.com --blast-only
```

### BLAST+ install location

| Platform | Path |
|---|---|
| Linux / macOS / BSD | `~/.local/blast/bin/` |
| Windows | `%USERPROFILE%\.local\blast\bin\` |

The bootstrap adds this directory to your shell profiles and the current session's `PATH` automatically (pass `--no-path` to suppress profile edits). The directory contains only the 6 binaries ScriptoScope needs — nothing else from the NCBI distribution is written to disk.

### Venv location

The pipx venv lives at:

| Platform | Path |
|---|---|
| Linux / macOS | `~/.local/pipx/venvs/scriptoscope/` |
| Windows | `%USERPROFILE%\.local\pipx\venvs\scriptoscope\` |

Activate it directly when you need an interactive shell inside the environment:

```sh
# Linux / macOS
source ~/.local/pipx/venvs/scriptoscope/bin/activate

# Windows (PowerShell)
& "$env:USERPROFILE\.local\pipx\venvs\scriptoscope\Scripts\Activate.ps1"
```

### Rebuilding developer-daemon.com after editing devtools/watch.py

The daemon rebuilds both APE binaries automatically when any file in `devtools/` changes. This means edits to `devtools/watch.py` itself trigger `sh devtools/build-ape.sh`, which writes the new `developer-daemon.com` to disk — restart the daemon to pick it up.

```sh
# After editing devtools/watch.py manually (or let the daemon do it):
sh devtools/build-ape.sh --no-daemon   # skip rebuilding developer-daemon.com
sh devtools/build-ape.sh               # rebuild both
```

### Useful pipx commands

| Command | Purpose |
|---|---|
| `pipx install . --force` | Reinstall after code changes |
| `pipx inject scriptoscope -r requirements.txt --force` | Sync dependency versions |
| `pipx inject scriptoscope pytest pytest-asyncio` | Add / update test tools |
| `pipx inject scriptoscope black isort` | Add formatting tools |
| `pipx runpip scriptoscope list` | List all packages in the venv |
| `pipx upgrade scriptoscope` | Upgrade from PyPI or current path |
| `pipx uninstall scriptoscope` | Remove completely |

---

## Environment Variables {#environment-variables}

| Variable | Default | Description |
|---|---|---|
| `SCRIPTOSCOPE_LOG` | `/tmp/scriptoscope.log` | Override the ScriptoScope log file location |
| `PIPX_HOME` | `~/.local/pipx` | Override the pipx home directory |
| `NO_COLOR` | _(unset)_ | Set to any value to disable ANSI colour in `setup-dev-env.com` and `watch.com` output |

### Custom log path

```sh
# Linux / macOS
export SCRIPTOSCOPE_LOG=~/logs/scriptoscope.log
scriptoscope

# Windows (PowerShell)
$env:SCRIPTOSCOPE_LOG = "$HOME\logs\scriptoscope.log"
scriptoscope
```

### Log rotation

The log is capped at 5 MB with 3 rotating backups (`scriptoscope.log.1`, `.log.2`, `.log.3`). To find a specific session:

```sh
# List recent session IDs
grep "session .* starting" /tmp/scriptoscope.log | tail

# Extract all lines from one session
grep "\[a1b2c3d4\]" /tmp/scriptoscope.log
```

---

## Troubleshooting {#troubleshooting}

### `developer-daemon.com` detects a change but the action fails

The most common cause is that `pipx` is not on `PATH` in the shell where `developer-daemon.com` is running. Open a new terminal (so the `pipx ensurepath` changes take effect) and start the daemon there. You can verify with:

```sh
which pipx
```

If `pipx` is missing entirely, run `setup-dev-env.com` again.

---

### `developer-daemon.com` doesn't detect changes on a network filesystem (NFS, SMB, FUSE)

Network filesystems often don't propagate kernel events (`kqueue`/`inotify`). Use polling mode:

```sh
./developer-daemon.com --poll --interval 1.0
```

---

### `run-detectors: unable to find an interpreter` on Linux

Some Linux systems route MZ-magic files to WINE via `binfmt_misc`. Run the binary via `sh` instead:

```sh
sh setup-dev-env.com
```

Or register the APE loader once to fix direct execution permanently:

```sh
sudo wget -O /usr/bin/ape https://cosmo.zip/pub/cosmos/bin/ape-$(uname -m).elf
sudo chmod +x /usr/bin/ape
sudo sh -c "echo ':APE:M::MZqFpD::/usr/bin/ape:' >/proc/sys/fs/binfmt_misc/register"
```

### `developer-daemon.com` not found after cloning

`developer-daemon.com` is built by `setup-dev-env.com` as its final step. If it's missing, either run setup again or build it directly:

```sh
# Option A: run setup (safe to re-run; rebuilds the daemon automatically)
_APE_ARGS="--no-tests --no-blast" ./setup-dev-env.com

# Option B: build both APE binaries directly (developer-only, needs curl + zip)
sh devtools/build-ape.sh
```

---

### `setup-dev-env.com` not found after cloning

`setup-dev-env.com` is committed to git and should always be present after `git clone`. If it is somehow missing, rebuild it:

```sh
sh devtools/build-ape.sh
```

This only requires `curl` (or `wget`) and `zip`. The ~35 MB Cosmopolitan Python is cached in `~/.cache/scriptoscope-ape/` after the first download.

### `BackendUnavailable: Cannot import 'setuptools.backends'`

The upstream `pyproject.toml` uses a non-standard build backend that was briefly added then removed from setuptools. `setup-dev-env.com` patches this automatically. To fix it manually:

```sh
sed -i.bak 's|setuptools.backends.legacy:build|setuptools.build_meta|' pyproject.toml
```

Or edit `pyproject.toml` directly:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

### `command not found: scriptoscope` after install

The pipx bin directory is not on your PATH. Run:

```sh
pipx ensurepath
# then restart your shell, or:
source ~/.bashrc   # or ~/.zshrc
```

### `ModuleNotFoundError: No module named 'pytest_asyncio'`

`pytest-asyncio` is not in the venv. Inject it:

```sh
pipx inject scriptoscope pytest-asyncio
```

### BLAST searches don't work

BLAST+ is installed automatically by the bootstrap to `~/.local/blast/bin`. If it's missing or not on your `PATH`, re-run the installer:

```sh
./setup-dev-env.com --blast-only
```

Then confirm the tools are on PATH (you may need to open a new terminal first):

```sh
blastn -version
blastp -version
```

If you need a specific version or a system-wide install, download manually from:

```
https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST/
```


### The TUI looks garbled or colours are wrong

Ensure your terminal supports 256 colours or true colour:

```sh
export TERM=xterm-256color
export COLORTERM=truecolor
```

### Inspecting crash logs

```sh
tail -200 /tmp/scriptoscope.log

# Follow live
tail -f /tmp/scriptoscope.log

# With a custom log path
tail -f "$SCRIPTOSCOPE_LOG"
```

Every session writes a startup banner with a unique 8-character session ID. All log lines are tagged with it, so you can isolate one session's output exactly.

---

## Contributing

### Code style

- Follow [PEP 8](https://peps.python.org/pep-0008/).
- Format with [Black](https://black.readthedocs.io/):
  ```sh
  pipx inject scriptoscope black
  ~/.local/pipx/venvs/scriptoscope/bin/black scriptoscope.py
  ```
- Sort imports with [isort](https://pycqa.github.io/isort/):
  ```sh
  pipx inject scriptoscope isort
  ~/.local/pipx/venvs/scriptoscope/bin/isort scriptoscope.py
  ```
- Type hints are expected throughout.

### Before submitting a PR

1. Run the full test suite and confirm all 105 tests pass:
   ```sh
   ~/.local/pipx/venvs/scriptoscope/bin/python -m pytest tests/ -q
   ```
2. Add tests for any new behaviour or bug fix.
3. Update `DEVELOPERS.md` if anything in the setup or workflow changes.
4. Keep commits focused — one logical change per commit.

### Commit message format

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add HMMER domain annotation panel
fix: correct off-by-one in sequence slice display
docs: update DEVELOPERS.md with APE binary workflow
test: add ORF cross-validation for reverse-strand frames
build: bump textual minimum to 8.2.2
```

### Opening issues

When reporting a bug, include:

- ScriptoScope version (`pipx list`)
- Python version (`python3 --version`)
- OS, architecture, and terminal emulator
- The relevant session from `/tmp/scriptoscope.log`
- Exact steps to reproduce

---

*If you find anything in this document out of date or unclear, please open an issue or submit a PR.*