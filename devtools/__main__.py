#!/usr/bin/env python3
"""
ScriptoScope Bootstrap — Actually Portable Executable entry point
=================================================================
This file is the __main__.py embedded inside setup-dev-env.com, an
Actually Portable Executable (APE) built with Cosmopolitan Python.

The binary runs natively on:
  - macOS   (x86_64 + ARM64 / Apple Silicon)
  - Linux   (x86_64 + ARM64, all major distros)
  - Windows (x86_64, via PE header)
  - FreeBSD / OpenBSD / NetBSD / DragonFlyBSD (x86_64 + ARM64)

Because this Python interpreter is self-contained inside the APE binary,
this script can always assume Python 3.12+ is available — no chicken-and-egg
problem. We use that interpreter to bootstrap the *system* Python (>= 3.10),
install pipx, and set up the ScriptoScope developer environment.

Usage (after building setup-dev-env.com):
  ./setup-dev-env.com                        # full setup
  ./setup-dev-env.com --no-tests             # skip test run
  ./setup-dev-env.com --python-only          # only install system Python
  ./setup-dev-env.com --version 3.12         # target a specific Python version
  ./setup-dev-env.com --help

On Linux, if ./setup-dev-env.com gives "run-detectors: unable to find an
interpreter", run it as:
  sh setup-dev-env.com
or register the APE loader:
  sudo wget -O /usr/bin/ape https://cosmo.zip/pub/cosmos/bin/ape-$(uname -m).elf
  sudo chmod +x /usr/bin/ape
  sudo sh -c "echo ':APE:M::MZqFpD::/usr/bin/ape:' >/proc/sys/fs/binfmt_misc/register"
"""

# ---------------------------------------------------------------------------
# stdlib only — this runs inside the bundled Cosmopolitan Python, before any
# system packages are available.
# ---------------------------------------------------------------------------
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Version targets
# ---------------------------------------------------------------------------
DEFAULT_PYTHON_VERSION = "3.13"
MIN_PYTHON_VERSION = "3.10"  # ScriptoScope requirement
PYPROJECT_MIN = (3, 10)

# ---------------------------------------------------------------------------
# Known latest patch releases — update as new CPython releases land.
# Used when downloading official installers.
# ---------------------------------------------------------------------------
KNOWN_PATCH: dict[str, str] = {
    "3.13": "3.13.3",
    "3.12": "3.12.10",
    "3.11": "3.11.12",
    "3.10": "3.10.17",
}

# ---------------------------------------------------------------------------
# BLAST+ configuration
# ---------------------------------------------------------------------------
BLAST_VERSION = "2.17.0+"
BLAST_VERSION_TAG = "2.17.0%2B"  # URL-encoded "+" for NCBI FTP paths

# The 6 binaries ScriptoScope actually invokes (from scriptoscope.py).
BLAST_BINS = ["blastn", "blastp", "blastx", "tblastn", "tblastx", "makeblastdb"]

# FTP base for LATEST — NCBI redirects /LATEST/ to the current release tarball.
_BLAST_BASE = "https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST"

# (os, normalized_arch) -> tarball filename
BLAST_TARBALLS: dict[tuple[str, str], str] = {
    ("macos", "arm64"): f"ncbi-blast-{BLAST_VERSION}-aarch64-macosx.tar.gz",
    ("macos", "x86_64"): f"ncbi-blast-{BLAST_VERSION}-x64-macosx.tar.gz",
    ("linux", "x86_64"): f"ncbi-blast-{BLAST_VERSION}-x64-linux.tar.gz",
    ("linux", "aarch64"): f"ncbi-blast-{BLAST_VERSION}-aarch64-linux.tar.gz",
    ("windows", "x86_64"): f"ncbi-blast-{BLAST_VERSION}-x64-win64.tar.gz",
    (
        "freebsd",
        "x86_64",
    ): f"ncbi-blast-{BLAST_VERSION}-x64-linux.tar.gz",  # Linux ELF runs under Linux compat
    ("freebsd", "aarch64"): f"ncbi-blast-{BLAST_VERSION}-aarch64-linux.tar.gz",
}


# Where the bootstrap installs BLAST+ binaries (inside the user's home dir,
# so no root required).  On Windows this becomes %USERPROFILE%\.local\blast\bin.
def _blast_install_dir() -> Path:
    if SYSINFO.os == "windows":
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        base = Path.home()
    return base / ".local" / "blast" / "bin"


# ---------------------------------------------------------------------------
# ANSI colour helpers (respects NO_COLOR env var and non-tty stdout)
# ---------------------------------------------------------------------------
def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if platform.system() == "Windows":
        return "WT_SESSION" in os.environ or os.environ.get("TERM") is not None
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_COLOR = _use_color()
_R = "\033[0m" if _COLOR else ""
_B = "\033[1m" if _COLOR else ""
_RD = "\033[31m" if _COLOR else ""
_GR = "\033[32m" if _COLOR else ""
_YL = "\033[33m" if _COLOR else ""
_CY = "\033[36m" if _COLOR else ""
_MG = "\033[35m" if _COLOR else ""


def info(msg: str) -> None:
    print(f"{_CY}[INFO]{_R}  {msg}")


def ok(msg: str) -> None:
    print(f"{_GR}[OK]{_R}    {msg}")


def warn(msg: str) -> None:
    print(f"{_YL}[WARN]{_R}  {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"{_RD}[ERROR]{_R} {msg}", file=sys.stderr)


def step(msg: str) -> None:
    print(f"\n{_B}{_MG}==>{_R}{_B} {msg}{_R}")


def header(msg: str) -> None:
    bar = "=" * (len(msg) + 4)
    print(f"\n{_B}{bar}{_R}")
    print(f"{_B}  {msg}{_R}")
    print(f"{_B}{bar}{_R}\n")


def die(msg: str, code: int = 1) -> None:
    err(msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="setup-dev-env.com",
        description="ScriptoScope cross-platform developer environment bootstrap.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            This binary is an Actually Portable Executable (APE).
            It runs natively on macOS, Linux, Windows, and BSD —
            no installation required.

            Examples:
              ./setup-dev-env.com
              ./setup-dev-env.com --no-tests
              ./setup-dev-env.com --version 3.12
              ./setup-dev-env.com --python-only
              ./setup-dev-env.com --no-blast
              ./setup-dev-env.com --no-daemon
              sh setup-dev-env.com          # if direct execute fails on Linux

            Exit codes:
              0  success
              1  setup failed
              130  interrupted (Ctrl-C)
        """),
    )
    p.add_argument(
        "--version",
        metavar="X.Y",
        default=DEFAULT_PYTHON_VERSION,
        help=f"System Python version to install (default: {DEFAULT_PYTHON_VERSION})",
    )
    p.add_argument(
        "--min-version",
        metavar="X.Y",
        default=MIN_PYTHON_VERSION,
        dest="min_version",
        help=f"Minimum acceptable system Python version (default: {MIN_PYTHON_VERSION})",
    )
    p.add_argument(
        "--python-only",
        action="store_true",
        help="Only install system Python; skip pipx and ScriptoScope setup",
    )
    p.add_argument(
        "--no-tests",
        action="store_true",
        help="Skip running the test suite after setup",
    )
    p.add_argument(
        "--force-pyenv",
        action="store_true",
        help="Force pyenv for system Python installation (Linux/BSD/macOS)",
    )
    p.add_argument(
        "--skip-python-check",
        action="store_true",
        help="Skip 'Python already installed' check and install unconditionally",
    )
    p.add_argument(
        "--no-path",
        action="store_true",
        help="Do not modify shell profiles or system PATH",
    )
    p.add_argument(
        "--no-blast",
        action="store_true",
        help="Skip downloading and installing BLAST+ command-line tools",
    )
    p.add_argument(
        "--blast-only",
        action="store_true",
        help="Only download and install BLAST+; skip all other setup steps",
    )
    p.add_argument(
        "--no-daemon",
        action="store_true",
        help="Skip building developer-daemon.com after setup",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress informational output",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
class SystemInfo:
    """Detected OS and Linux distro."""

    def __init__(self) -> None:
        raw = platform.system().lower()
        if raw == "darwin":
            self.os = "macos"
        elif raw == "windows":
            self.os = "windows"
        elif raw == "linux":
            self.os = "linux"
        elif "bsd" in raw or raw == "dragonfly":
            self.os = raw  # freebsd, openbsd, netbsd, dragonfly
        else:
            self.os = "unknown"

        self.arch = platform.machine().lower()  # x86_64, arm64/aarch64
        self.distro = self._detect_distro()
        self.machine = platform.node()

    def _detect_distro(self) -> str:
        if self.os != "linux":
            return ""
        # /etc/os-release is the authoritative source on modern systems.
        path = Path("/etc/os-release")
        if path.exists():
            pairs: dict[str, str] = {}
            for line in path.read_text().splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    pairs[k.strip()] = v.strip().strip('"')
            combined = (pairs.get("ID", "") + " " + pairs.get("ID_LIKE", "")).lower()
            if any(
                x in combined
                for x in (
                    "ubuntu",
                    "debian",
                    "mint",
                    "pop",
                    "raspbian",
                    "kali",
                    "elementary",
                )
            ):
                return "debian"
            if any(
                x in combined
                for x in ("fedora", "rhel", "centos", "rocky", "alma", "ol")
            ):
                return "fedora"
            if any(x in combined for x in ("arch", "manjaro", "endeavour", "garuda")):
                return "arch"
            if "alpine" in combined:
                return "alpine"
            if any(x in combined for x in ("suse", "opensuse")):
                return "suse"
            if "gentoo" in combined:
                return "gentoo"
            if "void" in combined:
                return "void"
        # Fallback: infer from available package managers.
        pm_map = [
            ("apt-get", "debian"),
            ("dnf", "fedora"),
            ("yum", "fedora"),
            ("pacman", "arch"),
            ("apk", "alpine"),
            ("zypper", "suse"),
            ("emerge", "gentoo"),
            ("xbps-install", "void"),
        ]
        for binary, distro in pm_map:
            if shutil.which(binary):
                return distro
        return ""

    @property
    def is_bsd(self) -> bool:
        return "bsd" in self.os or self.os == "dragonfly"

    def __str__(self) -> str:
        if self.os == "linux" and self.distro:
            return f"linux/{self.distro}"
        return self.os


SYSINFO = SystemInfo()


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------
def ver_tuple(s: str) -> tuple[int, ...]:
    """'3.13.1' -> (3, 13, 1).  '3.13' -> (3, 13)."""
    try:
        return tuple(int(x) for x in s.split(".") if x.isdigit())
    except ValueError:
        return (0,)


def ver_gte(a: str, b: str) -> bool:
    """Return True if version string a >= b (compares major.minor only)."""
    at = ver_tuple(a)[:2]
    bt = ver_tuple(b)[:2]
    return at >= bt


def ver_xy(version_output: str) -> str:
    """Extract 'X.Y' from 'Python 3.13.1' or similar."""
    import re

    m = re.search(r"(\d+\.\d+)", version_output)
    return m.group(1) if m else "0.0"


# ---------------------------------------------------------------------------
# Shell execution helpers
# ---------------------------------------------------------------------------
def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, streaming output unless capture=True."""
    env = {**os.environ, **(extra_env or {})}
    info(f"$ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        input=input_text,
        env=env,
    )


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


# ---------------------------------------------------------------------------
# Privilege escalation — sudo / doas / runas
# ---------------------------------------------------------------------------
def _priv_prefix() -> list[str]:
    """Return ['sudo'] / ['doas'] / [] depending on context."""
    if os.getuid() == 0:  # type: ignore[attr-defined]
        return []
    if have("sudo"):
        return ["sudo"]
    if have("doas"):
        return ["doas"]
    warn("Not running as root and no sudo/doas found. Package installation may fail.")
    return []


# On Windows os.getuid doesn't exist; monkey-patch a safe shim.
if SYSINFO.os == "windows":
    os.getuid = lambda: 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Find a suitable system Python already on PATH
# ---------------------------------------------------------------------------
def find_system_python(min_ver: str) -> str | None:
    """Return the first python executable that satisfies min_ver, or None."""
    candidates = [
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
        "python3",
        "python",
    ]
    # Also probe well-known Windows paths.
    if SYSINFO.os == "windows":
        local = os.environ.get("LOCALAPPDATA", "")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        for d in ("313", "312", "311", "310"):
            candidates += [
                rf"{local}\Programs\Python\Python{d}\python.exe",
                rf"{pf}\Python{d}\python.exe",
                rf"C:\Python{d}\python.exe",
            ]

    for cand in candidates:
        exe = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if not exe:
            continue
        try:
            r = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=5
            )
            out = (r.stdout + r.stderr).strip()
            xy = ver_xy(out)
            if ver_gte(xy, min_ver):
                return exe
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# PATH / shell profile helpers
# ---------------------------------------------------------------------------
def add_to_path(directory: str, *, no_path: bool) -> None:
    """Append directory to PATH in current session and common shell profiles."""
    if no_path or not Path(directory).is_dir():
        return
    # Current session.
    existing = os.environ.get("PATH", "")
    if directory not in existing:
        os.environ["PATH"] = f"{directory}{os.pathsep}{existing}"
    # Persist to shell profiles.
    profiles = []
    home = Path.home()
    for name in (".bashrc", ".bash_profile", ".zshrc", ".profile"):
        p = home / name
        if p.exists():
            profiles.append(p)
    snippet = f'\n# Added by setup-dev-env.com\nexport PATH="{directory}:$PATH"\n'
    for prof in profiles:
        try:
            text = prof.read_text()
            if directory not in text:
                prof.write_text(text + snippet)
                info(f"Updated PATH in {prof}")
        except OSError:
            pass


def add_pyenv_to_profiles(pyenv_root: str, *, no_path: bool) -> None:
    if no_path:
        return
    snippet = textwrap.dedent(f"""
        # pyenv — added by setup-dev-env.com
        export PYENV_ROOT="{pyenv_root}"
        export PATH="$PYENV_ROOT/bin:$PATH"
        eval "$(pyenv init -)"
    """)
    home = Path.home()
    for name in (".bashrc", ".bash_profile", ".zshrc", ".profile"):
        prof = home / name
        if prof.exists():
            try:
                text = prof.read_text()
                if "pyenv init" not in text:
                    prof.write_text(text + snippet)
                    info(f"Added pyenv init to {prof}")
            except OSError:
                pass


# ===========================================================================
# ███████╗███████╗ ██████╗████████╗██╗ ██████╗ ███╗   ██╗    ██╗
# ██╔════╝██╔════╝██╔════╝╚══██╔══╝██║██╔═══██╗████╗  ██║    ██║
# ███████╗█████╗  ██║        ██║   ██║██║   ██║██╔██╗ ██║    ██║
# ╚════██║██╔══╝  ██║        ██║   ██║██║   ██║██║╚██╗██║    ╚═╝
# ███████║███████╗╚██████╗   ██║   ██║╚██████╔╝██║ ╚████║    ██╗
# ╚══════╝╚══════╝ ╚═════╝   ╚═╝   ╚═╝ ╚═════╝ ╚═╝  ╚═══╝    ╚═╝
#
# SYSTEM PYTHON INSTALLATION
# One function per OS/distro.  All fall back to pyenv on failure.
# ===========================================================================


# ---------------------------------------------------------------------------
# pyenv — universal fallback for Linux, BSD, and macOS
# ---------------------------------------------------------------------------
def _pyenv_build_deps() -> None:
    """Install C build dependencies required by pyenv's CPython build."""
    priv = _priv_prefix()
    info("Installing pyenv build dependencies...")
    try:
        if SYSINFO.os == "linux":
            d = SYSINFO.distro
            if d == "debian":
                run(priv + ["apt-get", "update", "-qq"])
                run(
                    priv
                    + [
                        "apt-get",
                        "install",
                        "-y",
                        "build-essential",
                        "libssl-dev",
                        "zlib1g-dev",
                        "libbz2-dev",
                        "libreadline-dev",
                        "libsqlite3-dev",
                        "libncursesw5-dev",
                        "xz-utils",
                        "tk-dev",
                        "libxml2-dev",
                        "libxmlsec1-dev",
                        "libffi-dev",
                        "liblzma-dev",
                        "curl",
                        "git",
                    ]
                )
            elif d == "fedora":
                mgr = "dnf" if have("dnf") else "yum"
                run(
                    priv + [mgr, "groupinstall", "-y", "Development Tools"], check=False
                )
                run(
                    priv
                    + [
                        mgr,
                        "install",
                        "-y",
                        "openssl-devel",
                        "bzip2-devel",
                        "libffi-devel",
                        "zlib-devel",
                        "readline-devel",
                        "sqlite-devel",
                        "xz-devel",
                        "tk-devel",
                        "ncurses-devel",
                        "curl",
                        "git",
                    ]
                )
            elif d == "arch":
                run(
                    priv
                    + [
                        "pacman",
                        "-Sy",
                        "--noconfirm",
                        "--needed",
                        "base-devel",
                        "openssl",
                        "zlib",
                        "bzip2",
                        "readline",
                        "sqlite",
                        "ncurses",
                        "tk",
                        "xz",
                        "libffi",
                        "curl",
                        "git",
                    ]
                )
            elif d == "alpine":
                run(
                    priv
                    + [
                        "apk",
                        "add",
                        "--no-cache",
                        "build-base",
                        "openssl-dev",
                        "bzip2-dev",
                        "zlib-dev",
                        "readline-dev",
                        "sqlite-dev",
                        "ncurses-dev",
                        "tk-dev",
                        "xz-dev",
                        "libffi-dev",
                        "curl",
                        "git",
                    ]
                )
            elif d == "suse":
                run(
                    priv
                    + [
                        "zypper",
                        "install",
                        "-y",
                        "gcc",
                        "make",
                        "openssl-devel",
                        "zlib-devel",
                        "bzip2-devel",
                        "readline-devel",
                        "sqlite3-devel",
                        "libffi-devel",
                        "ncurses-devel",
                        "tk-devel",
                        "xz-devel",
                        "curl",
                        "git",
                    ]
                )
            else:
                warn("Unknown distro — skipping build dep install.")
        elif SYSINFO.os == "freebsd":
            run(
                priv
                + [
                    "pkg",
                    "install",
                    "-y",
                    "openssl",
                    "bzip2",
                    "readline",
                    "sqlite3",
                    "libffi",
                    "lzma",
                    "tk85",
                    "curl",
                    "git",
                ]
            )
        elif SYSINFO.os in ("openbsd", "netbsd", "dragonfly"):
            warn(
                "BSD build deps not auto-installed. "
                "Install openssl, readline, sqlite3, libffi, curl, git manually if needed."
            )
    except subprocess.CalledProcessError as exc:
        warn(
            f"Build dep install partially failed (exit {exc.returncode}). "
            "pyenv build may still succeed."
        )


def install_via_pyenv(version: str, *, no_path: bool) -> str | None:
    """Install Python via pyenv. Returns the path to the new python binary."""
    step(f"Installing Python {version} via pyenv")
    _pyenv_build_deps()

    pyenv_root = os.environ.get("PYENV_ROOT", str(Path.home() / ".pyenv"))

    # Install or update pyenv.
    if Path(pyenv_root).is_dir():
        info(f"pyenv found at {pyenv_root} — updating...")
        try:
            run(["git", "-C", pyenv_root, "pull", "--ff-only"], check=False)
        except Exception:
            pass
    else:
        info(f"Cloning pyenv into {pyenv_root}...")
        downloader = "curl" if have("curl") else ("wget" if have("wget") else None)
        if not downloader:
            die("curl or wget is required to install pyenv.")
        if downloader == "curl":
            r = run(["curl", "-fsSL", "https://pyenv.run"], capture=True)
        else:
            r = run(["wget", "-qO-", "https://pyenv.run"], capture=True)
        subprocess.run(["sh"], input=r.stdout, text=True, check=True)

    # Put pyenv on PATH for the current process.
    pyenv_bin = str(Path(pyenv_root) / "bin")
    if pyenv_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = pyenv_bin + os.pathsep + os.environ.get("PATH", "")
    os.environ["PYENV_ROOT"] = pyenv_root

    if not have("pyenv"):
        die("pyenv installation failed or is not on PATH.")

    # Initialise pyenv shims in the current shell.
    try:
        r = run(["pyenv", "init", "-"], capture=True, check=False)
        for line in r.stdout.splitlines():
            if line.startswith("export "):
                k, _, v = line[7:].partition("=")
                os.environ[k] = v.strip('"').strip("'")
    except Exception:
        pass

    # Install the requested version.
    installed_versions = run(
        ["pyenv", "versions", "--bare"], capture=True, check=False
    ).stdout
    if version in installed_versions:
        info(f"Python {version} already installed in pyenv — skipping build.")
    else:
        info(f"Building Python {version} (this may take several minutes)...")
        run(["pyenv", "install", version])

    run(["pyenv", "global", version])
    ok(f"Python {version} set as pyenv global")
    add_pyenv_to_profiles(pyenv_root, no_path=no_path)

    pyenv_python = str(Path(pyenv_root) / "shims" / "python3")
    return pyenv_python if Path(pyenv_python).exists() else None


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------
def _ensure_homebrew() -> bool:
    """Return True if brew is available (installing it if not)."""
    for prefix in ("/opt/homebrew", "/usr/local"):
        brew = Path(prefix) / "bin" / "brew"
        if brew.exists():
            # Make sure brew is on PATH.
            add_to_path(str(brew.parent), no_path=False)
            return True
    if have("brew"):
        return True
    info("Homebrew not found — installing it now...")
    try:
        r = run(
            [
                "curl",
                "-fsSL",
                "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh",
            ],
            capture=True,
        )
        subprocess.run(["/bin/bash"], input=r.stdout, text=True, check=True)
        # Source brew shellenv so it's on PATH immediately.
        for prefix in ("/opt/homebrew", "/usr/local"):
            brew = Path(prefix) / "bin" / "brew"
            if brew.exists():
                add_to_path(str(brew.parent), no_path=False)
                return True
    except subprocess.CalledProcessError:
        warn("Homebrew install failed.")
    return False


def install_macos(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on macOS")

    if _ensure_homebrew():
        pkg = f"python@{version}"
        info(f"Running: brew install {pkg}")
        try:
            run(["brew", "install", pkg])
            # Homebrew Python is not linked to /usr/local/bin by default.
            for prefix in ("/opt/homebrew", "/usr/local"):
                py_bin = Path(prefix) / "opt" / pkg / "libexec" / "bin"
                if py_bin.is_dir():
                    add_to_path(str(py_bin), no_path=no_path)
                    ok(f"Python {version} installed via Homebrew")
                    return str(py_bin / "python3")
        except subprocess.CalledProcessError:
            warn(f"brew install {pkg} failed.")

    # Fallback: official python.org .pkg installer.
    return _install_macos_official(version, no_path=no_path)


def _install_macos_official(version: str, *, no_path: bool) -> str | None:
    full = KNOWN_PATCH.get(version, f"{version}.0")
    url = f"https://www.python.org/ftp/python/{full}/python-{full}-macos11.pkg"
    dest = f"/tmp/python-{full}.pkg"
    info(f"Downloading Python {full} from python.org...")
    try:
        run(["curl", "-fsSL", "-o", dest, url])
        priv = _priv_prefix()
        run(priv + ["installer", "-pkg", dest, "-target", "/"])
        Path(dest).unlink(missing_ok=True)
        fw_bin = f"/Library/Frameworks/Python.framework/Versions/{version}/bin"
        add_to_path(fw_bin, no_path=no_path)
        ok(f"Python {full} installed via python.org installer")
        return str(Path(fw_bin) / f"python{version}")
    except subprocess.CalledProcessError:
        warn("python.org installer failed.")
        return None


# ---------------------------------------------------------------------------
# Linux — Debian / Ubuntu family
# ---------------------------------------------------------------------------
def install_linux_debian(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on Debian/Ubuntu")
    priv = _priv_prefix()
    pkg = f"python{version}"

    run(priv + ["apt-get", "update", "-qq"])

    # Try the versioned package first.
    r = run(
        priv
        + [
            "apt-get",
            "install",
            "-y",
            pkg,
            f"python{version}-venv",
            f"python{version}-pip",
        ],
        check=False,
    )
    if r.returncode == 0:
        ok(f"Installed {pkg} via apt")
        return shutil.which(f"python{version}") or shutil.which("python3")

    # Detect release family for PPA / backports.
    info(f"{pkg} not in default repos — trying extended sources...")
    release_id = ""
    try:
        r2 = run(["lsb_release", "-si"], capture=True, check=False)
        release_id = r2.stdout.strip().lower()
    except Exception:
        pass
    if not release_id:
        id_like = ""
        os_release = Path("/etc/os-release")
        if os_release.exists():
            for line in os_release.read_text().splitlines():
                if line.startswith("ID="):
                    release_id = line.split("=", 1)[1].strip('"').lower()
                if line.startswith("ID_LIKE="):
                    id_like = line.split("=", 1)[1].strip('"').lower()
        if not release_id:
            release_id = id_like

    if any(x in release_id for x in ("ubuntu", "mint", "pop")):
        info("Adding deadsnakes PPA...")
        try:
            run(priv + ["apt-get", "install", "-y", "software-properties-common"])
            run(priv + ["add-apt-repository", "-y", "ppa:deadsnakes/ppa"])
            run(priv + ["apt-get", "update", "-qq"])
            run(
                priv
                + [
                    "apt-get",
                    "install",
                    "-y",
                    pkg,
                    f"python{version}-venv",
                    f"python{version}-distutils",
                ]
            )
            ok(f"Installed {pkg} via deadsnakes PPA")
            return shutil.which(f"python{version}") or shutil.which("python3")
        except subprocess.CalledProcessError:
            warn("deadsnakes PPA install failed.")
    elif "debian" in release_id:
        info("Trying Debian backports...")
        try:
            codename_r = run(
                ["sh", "-c", ". /etc/os-release && echo ${VERSION_CODENAME:-}"],
                capture=True,
                check=False,
            )
            codename = codename_r.stdout.strip()
            if codename:
                backports_line = (
                    f"deb http://deb.debian.org/debian {codename}-backports main"
                )
                bp_file = Path("/etc/apt/sources.list.d/backports.list")
                run(priv + ["sh", "-c", f"echo '{backports_line}' > {bp_file}"])
                run(priv + ["apt-get", "update", "-qq"])
                run(
                    priv
                    + ["apt-get", "install", "-y", "-t", f"{codename}-backports", pkg]
                )
                ok(f"Installed {pkg} via Debian backports")
                return shutil.which(f"python{version}") or shutil.which("python3")
        except subprocess.CalledProcessError:
            warn("Debian backports install failed.")

    warn(f"Could not install Python {version} via apt. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# Linux — Fedora / RHEL / CentOS family
# ---------------------------------------------------------------------------
def install_linux_fedora(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on Fedora/RHEL")
    priv = _priv_prefix()
    mgr = "dnf" if have("dnf") else ("yum" if have("yum") else None)
    if not mgr:
        warn("No dnf or yum found.")
        return install_via_pyenv(version, no_path=no_path)

    minor = version.split(".")[1]
    pkg_dot = f"python{version}"  # python3.13
    pkg_nodot = f"python{version.replace('.', '')}"  # python313

    for pkg in (pkg_dot, pkg_nodot):
        r = run(priv + [mgr, "install", "-y", pkg], check=False)
        if r.returncode == 0:
            ok(f"Installed {pkg} via {mgr}")
            return shutil.which(pkg_dot) or shutil.which("python3")

    # Try dnf module streams (RHEL 8/9).
    for stream in (f"python3{minor}", f"python:{version}"):
        r1 = run(priv + [mgr, "module", "enable", "-y", stream], check=False)
        r2 = run(priv + [mgr, "module", "install", "-y", stream], check=False)
        if r1.returncode == 0 or r2.returncode == 0:
            run(priv + [mgr, "install", "-y", pkg_dot], check=False)
            ok(f"Installed Python {version} via {mgr} module stream")
            return shutil.which(pkg_dot) or shutil.which("python3")

    warn(f"Could not install Python {version} via {mgr}. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# Linux — Arch family
# ---------------------------------------------------------------------------
def install_linux_arch(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on Arch Linux")
    priv = _priv_prefix()

    # 'python' in core tracks the latest stable Python 3.
    try:
        r = run(["pacman", "-Si", "python"], capture=True, check=False)
        for line in r.stdout.splitlines():
            if line.startswith("Version"):
                arch_ver = ver_xy(line)
                if ver_gte(arch_ver, version):
                    run(priv + ["pacman", "-Sy", "--noconfirm", "python", "python-pip"])
                    ok("Installed python via pacman")
                    return shutil.which("python3") or shutil.which("python")
    except Exception:
        pass

    # Try AUR helpers for older/versioned packages.
    pkg = f"python{version.replace('.', '')}"
    for aur in ("yay", "paru"):
        if have(aur):
            try:
                run([aur, "-Sy", "--noconfirm", pkg])
                ok(f"Installed {pkg} via {aur} (AUR)")
                return shutil.which(f"python{version}") or shutil.which("python3")
            except subprocess.CalledProcessError:
                warn(f"{aur} failed.")

    warn("No suitable Arch package found. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# Linux — Alpine
# ---------------------------------------------------------------------------
def install_linux_alpine(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on Alpine Linux")
    priv = _priv_prefix()
    run(priv + ["apk", "update"])
    run(priv + ["apk", "add", "python3", "py3-pip"])
    installed_ver = ver_xy(
        subprocess.run(["python3", "--version"], capture_output=True, text=True).stdout
        + subprocess.run(
            ["python3", "--version"], capture_output=True, text=True
        ).stderr
    )
    if ver_gte(installed_ver, MIN_PYTHON_VERSION):
        ok(f"Installed python3 ({installed_ver}) via apk")
        return shutil.which("python3")
    warn("apk python3 too old. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# Linux — openSUSE / SLES
# ---------------------------------------------------------------------------
def install_linux_suse(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on openSUSE/SLES")
    priv = _priv_prefix()
    pkg = f"python{version.replace('.', '')}"  # python313

    r = run(priv + ["zypper", "install", "-y", pkg], check=False)
    if r.returncode == 0:
        ok(f"Installed {pkg} via zypper")
        return shutil.which(f"python{version}") or shutil.which("python3")

    # Try the OBS devel:languages:python repository.
    info("Adding OBS devel:languages:python repository...")
    try:
        os_ver_r = run(
            ["sh", "-c", ". /etc/os-release && echo ${VERSION_ID:-Tumbleweed}"],
            capture=True,
            check=False,
        )
        os_ver = os_ver_r.stdout.strip() or "Tumbleweed"
        obs_url = f"https://download.opensuse.org/repositories/devel:/languages:/python/{os_ver}/"
        run(
            priv + ["zypper", "addrepo", "--refresh", obs_url, "devel-python"],
            check=False,
        )
        run(priv + ["zypper", "--gpg-auto-import-keys", "refresh"])
        run(priv + ["zypper", "install", "-y", pkg])
        ok(f"Installed {pkg} via OBS repo")
        return shutil.which(f"python{version}") or shutil.which("python3")
    except subprocess.CalledProcessError:
        warn("OBS repo install failed. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# Linux — Gentoo
# ---------------------------------------------------------------------------
def install_linux_gentoo(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on Gentoo")
    priv = _priv_prefix()
    try:
        r = run(priv + ["emerge", "--ask=n", f"dev-lang/python:{version}"], check=False)
        if r.returncode != 0:
            run(priv + ["emerge", "--ask=n", f"=dev-lang/python-{version}*"])
        ok(f"Python {version} installed via emerge")
        return shutil.which(f"python{version}") or shutil.which("python3")
    except subprocess.CalledProcessError:
        warn("emerge failed. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# Linux — Void
# ---------------------------------------------------------------------------
def install_linux_void(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on Void Linux")
    priv = _priv_prefix()
    run(priv + ["xbps-install", "-Sy", "python3", "python3-pip"])
    installed_ver = ver_xy(
        subprocess.run(
            ["python3", "--version"], capture_output=True, text=True, timeout=5
        ).stdout
    )
    if ver_gte(installed_ver, MIN_PYTHON_VERSION):
        ok(f"Installed python3 ({installed_ver}) via xbps")
        return shutil.which("python3")
    warn("xbps python3 too old. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# FreeBSD
# ---------------------------------------------------------------------------
def install_freebsd(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on FreeBSD")
    priv = _priv_prefix()
    pkg = f"python{version.replace('.', '')}"  # python313
    r = run(priv + ["pkg", "install", "-y", pkg], check=False)
    if r.returncode == 0:
        ok(f"Installed {pkg} via pkg")
        return shutil.which(f"python{version}") or shutil.which("python3")
    r2 = run(priv + ["pkg", "install", "-y", "python3"], check=False)
    if r2.returncode == 0:
        installed_ver = ver_xy(
            subprocess.run(
                ["python3", "--version"], capture_output=True, text=True
            ).stdout
        )
        if ver_gte(installed_ver, MIN_PYTHON_VERSION):
            ok(f"Installed python3 ({installed_ver}) via pkg")
            return shutil.which("python3")
    warn("pkg install failed. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# OpenBSD
# ---------------------------------------------------------------------------
def install_openbsd(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on OpenBSD")
    priv = _priv_prefix()
    # OpenBSD uses flavour syntax: "python%3.13"
    r = run(priv + ["pkg_add", f"python%{version}"], check=False)
    if r.returncode == 0:
        ok("Installed python via pkg_add")
        add_to_path("/usr/local/bin", no_path=no_path)
        return shutil.which(f"python{version}") or shutil.which("python3")
    r2 = run(priv + ["pkg_add", "python3"], check=False)
    if r2.returncode == 0:
        installed_ver = ver_xy(
            subprocess.run(
                ["python3", "--version"], capture_output=True, text=True
            ).stdout
        )
        if ver_gte(installed_ver, MIN_PYTHON_VERSION):
            ok(f"Installed python3 ({installed_ver}) via pkg_add")
            return shutil.which("python3")
    warn("pkg_add failed. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# NetBSD
# ---------------------------------------------------------------------------
def install_netbsd(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on NetBSD")
    priv = _priv_prefix()
    nodot = version.replace(".", "")
    pkg = f"python{nodot}"
    if have("pkgin"):
        run(priv + ["pkgin", "update"], check=False)
        r = run(priv + ["pkgin", "-y", "install", pkg], check=False)
        if r.returncode == 0:
            ok(f"Installed {pkg} via pkgin")
            return shutil.which(f"python{version}") or shutil.which("python3")
    r2 = run(priv + ["pkg_add", pkg], check=False)
    if r2.returncode == 0:
        ok(f"Installed {pkg} via pkg_add")
        return shutil.which(f"python{version}") or shutil.which("python3")
    warn("pkgin/pkg_add failed. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# DragonFlyBSD
# ---------------------------------------------------------------------------
def install_dragonfly(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on DragonFlyBSD")
    priv = _priv_prefix()
    pkg = f"python{version.replace('.', '')}"
    r = run(priv + ["pkg", "install", "-y", pkg], check=False)
    if r.returncode == 0:
        ok(f"Installed {pkg} via dports pkg")
        return shutil.which(f"python{version}") or shutil.which("python3")
    warn("dports pkg failed. Falling back to pyenv.")
    return install_via_pyenv(version, no_path=no_path)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
def install_windows(version: str, *, no_path: bool) -> str | None:
    step(f"Installing Python {version} on Windows")

    # 1. winget (built into Windows 10 1709+ and all Windows 11).
    if have("winget"):
        parts = version.split(".")
        pkg_id = f"Python.Python.{parts[0]}.{parts[1]}"  # Python.Python.3.13
        info(f"Trying winget install {pkg_id}...")
        r = subprocess.run(
            [
                "winget",
                "install",
                "--id",
                pkg_id,
                "--exact",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            text=True,
            check=False,
        )
        if r.returncode in (0, -1978335189):  # 0=success, hex=already installed
            ok(f"Python {version} installed via winget")
            _refresh_windows_path()
            found = find_system_python(MIN_PYTHON_VERSION)
            if found:
                return found

    # 2. Scoop.
    if have("scoop"):
        nodot = version.replace(".", "")
        info(f"Trying scoop install python{nodot}...")
        try:
            subprocess.run(
                ["scoop", "bucket", "add", "versions"], capture_output=True, check=False
            )
            r = subprocess.run(
                ["scoop", "install", f"python{nodot}"], text=True, check=False
            )
            if r.returncode == 0:
                ok(f"Python {version} installed via Scoop")
                _refresh_windows_path()
                found = find_system_python(MIN_PYTHON_VERSION)
                if found:
                    return found
        except Exception:
            pass

    # 3. Chocolatey.
    if have("choco"):
        nodot = version.replace(".", "")
        info(f"Trying choco install python{nodot}...")
        r = subprocess.run(
            ["choco", "install", f"python{nodot}", "-y", "--no-progress"],
            text=True,
            check=False,
        )
        if r.returncode == 0:
            ok(f"Python {version} installed via Chocolatey")
            _refresh_windows_path()
            found = find_system_python(MIN_PYTHON_VERSION)
            if found:
                return found

    # 4. Official python.org EXE installer (universal fallback).
    return _install_windows_official(version, no_path=no_path)


def _install_windows_official(version: str, *, no_path: bool) -> str | None:
    full = KNOWN_PATCH.get(version, f"{version}.0")
    arch_str = "amd64" if platform.machine().lower() in ("x86_64", "amd64") else "win32"
    url = f"https://www.python.org/ftp/python/{full}/python-{full}-{arch_str}.exe"
    dest = Path(os.environ.get("TEMP", r"C:\Temp")) / f"python-{full}.exe"

    info(f"Downloading Python {full} from python.org...")
    try:
        import urllib.request

        urllib.request.urlretrieve(url, str(dest))
    except Exception as exc:
        warn(f"Download failed: {exc}")
        warn(
            f"Please download manually from: https://www.python.org/downloads/windows/"
        )
        return None

    path_flag = "1" if not no_path else "0"
    info("Running installer (a UAC prompt may appear)...")
    r = subprocess.run(
        [
            str(dest),
            "/quiet",
            "InstallAllUsers=0",
            f"PrependPath={path_flag}",
            "Include_pip=1",
            "Include_test=0",
            "Include_launcher=1",
        ],
        check=False,
    )
    dest.unlink(missing_ok=True)
    if r.returncode == 0:
        ok(f"Python {full} installed via python.org installer")
        _refresh_windows_path()
        return find_system_python(MIN_PYTHON_VERSION)
    warn(f"Installer exited with code {r.returncode}.")
    return None


def _refresh_windows_path() -> None:
    """Re-read user and machine PATH into the current process environment."""
    try:
        import winreg  # type: ignore

        for root, scope in (
            (winreg.HKEY_CURRENT_USER, "user"),
            (winreg.HKEY_LOCAL_MACHINE, "machine"),
        ):
            try:
                key = winreg.OpenKey(
                    root,
                    r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
                    if scope == "machine"
                    else r"Environment",
                )
                path_val, _ = winreg.QueryValueEx(key, "PATH")
                winreg.CloseKey(key)
                existing = os.environ.get("PATH", "")
                for p in path_val.split(os.pathsep):
                    if p and p not in existing:
                        os.environ["PATH"] = p + os.pathsep + existing
                        existing = os.environ["PATH"]
            except Exception:
                pass
    except ImportError:
        pass


# ===========================================================================
# SYSTEM PYTHON INSTALL DISPATCHER
# ===========================================================================
def ensure_system_python(args: argparse.Namespace) -> str:
    """
    Return a path to a system Python >= args.min_version, installing
    one if necessary.
    """
    step("Checking for system Python")

    if not args.skip_python_check and not args.force_pyenv:
        existing = find_system_python(args.min_version)
        if existing:
            try:
                ver_out = subprocess.run(
                    [existing, "--version"], capture_output=True, text=True
                ).stdout
                ver_out += subprocess.run(
                    [existing, "--version"], capture_output=True, text=True
                ).stderr
            except Exception:
                ver_out = "unknown"
            ok(
                f"System Python already satisfies >= {args.min_version}: "
                f"{ver_out.strip()}  ({existing})"
            )
            return existing

    info(
        f"System Python >= {args.min_version} not found — installing Python {args.version}..."
    )

    if args.force_pyenv:
        result = install_via_pyenv(args.version, no_path=args.no_path)
    else:
        os_ = SYSINFO.os
        dist = SYSINFO.distro
        dispatch: dict[str, object] = {
            "macos": lambda: install_macos(args.version, no_path=args.no_path),
            "windows": lambda: install_windows(args.version, no_path=args.no_path),
            "freebsd": lambda: install_freebsd(args.version, no_path=args.no_path),
            "openbsd": lambda: install_openbsd(args.version, no_path=args.no_path),
            "netbsd": lambda: install_netbsd(args.version, no_path=args.no_path),
            "dragonfly": lambda: install_dragonfly(args.version, no_path=args.no_path),
        }
        if os_ in dispatch:
            result = dispatch[os_]()  # type: ignore[operator]
        elif os_ == "linux":
            linux_dispatch = {
                "debian": lambda: install_linux_debian(
                    args.version, no_path=args.no_path
                ),
                "fedora": lambda: install_linux_fedora(
                    args.version, no_path=args.no_path
                ),
                "arch": lambda: install_linux_arch(args.version, no_path=args.no_path),
                "alpine": lambda: install_linux_alpine(
                    args.version, no_path=args.no_path
                ),
                "suse": lambda: install_linux_suse(args.version, no_path=args.no_path),
                "gentoo": lambda: install_linux_gentoo(
                    args.version, no_path=args.no_path
                ),
                "void": lambda: install_linux_void(args.version, no_path=args.no_path),
            }
            if dist in linux_dispatch:
                result = linux_dispatch[dist]()  # type: ignore[operator]
            else:
                warn(f"Unknown Linux distro '{dist}'. Falling back to pyenv.")
                result = install_via_pyenv(args.version, no_path=args.no_path)
        else:
            die(
                f"Unsupported platform: {SYSINFO.os}. "
                f"Install Python {args.version}+ manually from https://www.python.org/downloads/"
            )

    # Re-probe PATH after install.
    found = result or find_system_python(args.min_version)
    if not found:
        die(
            f"Python >= {args.min_version} still not found after installation.\n"
            "Please open a new terminal (so PATH changes take effect) and retry,\n"
            "or install Python manually from https://www.python.org/downloads/",
        )
    ver_r = subprocess.run([found, "--version"], capture_output=True, text=True)
    ok(f"System Python ready: {(ver_r.stdout + ver_r.stderr).strip()}  ({found})")
    return found


# ===========================================================================
# PIPX
# ===========================================================================
def ensure_pipx(system_python: str, *, no_path: bool) -> None:
    step("Ensuring pipx is installed")

    if have("pipx"):
        ok("pipx is already on PATH")
        run(["pipx", "ensurepath"], check=False)
        return

    info("pipx not found — installing...")
    installed = False
    os_ = SYSINFO.os

    if os_ == "macos" and have("brew"):
        try:
            run(["brew", "install", "pipx"])
            installed = True
        except subprocess.CalledProcessError:
            warn("brew install pipx failed.")

    elif os_ == "linux":
        dist = SYSINFO.distro
        priv = _priv_prefix()
        if dist == "debian" and have("apt-get"):
            try:
                run(priv + ["apt-get", "install", "-y", "pipx"])
                installed = True
            except subprocess.CalledProcessError:
                warn("apt install pipx failed.")
        elif dist == "fedora" and (have("dnf") or have("yum")):
            mgr = "dnf" if have("dnf") else "yum"
            try:
                run(priv + [mgr, "install", "-y", "pipx"])
                installed = True
            except subprocess.CalledProcessError:
                warn(f"{mgr} install pipx failed.")
        elif dist == "arch" and have("pacman"):
            try:
                run(priv + ["pacman", "-Sy", "--noconfirm", "python-pipx"])
                installed = True
            except subprocess.CalledProcessError:
                warn("pacman install python-pipx failed.")

    elif SYSINFO.is_bsd and SYSINFO.os == "freebsd" and have("pkg"):
        try:
            run(_priv_prefix() + ["pkg", "install", "-y", "py311-pipx"])
            installed = True
        except subprocess.CalledProcessError:
            warn("pkg install py311-pipx failed.")

    elif os_ == "windows" and have("scoop"):
        try:
            run(["scoop", "install", "pipx"])
            installed = True
        except subprocess.CalledProcessError:
            warn("scoop install pipx failed.")

    if not installed:
        # Universal fallback: pip install --user pipx
        info("Installing pipx via pip --user...")
        run([system_python, "-m", "pip", "install", "--user", "pipx"])

    # Ensure pipx bin dir is on PATH.
    run([system_python, "-m", "pipx", "ensurepath"], check=False)
    if have("pipx"):
        run(["pipx", "ensurepath"], check=False)

    if not have("pipx"):
        warn(
            "pipx is not yet on PATH in this terminal session.\n"
            "  Open a new terminal after setup completes, or run:\n"
            f"    {system_python} -m pipx ensurepath"
        )
    else:
        ok("pipx installed and on PATH")


# ===========================================================================
# REPO / BUILD-BACKEND HELPERS
# ===========================================================================
LEGACY_BACKEND = "setuptools.backends.legacy:build"
CORRECT_BACKEND = "setuptools.build_meta"


def find_repo_root() -> Path:
    """Walk up from this script's location to find pyproject.toml."""
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists():
        return cwd
    die(
        "Cannot locate the ScriptoScope repository root.\n"
        "Run this binary from inside the cloned ScriptoScope directory."
    )


def fix_build_backend(repo: Path) -> None:
    step("Checking pyproject.toml build backend")
    pyproject = repo / "pyproject.toml"
    if not pyproject.exists():
        warn("pyproject.toml not found — skipping.")
        return
    content = pyproject.read_text(encoding="utf-8")
    if LEGACY_BACKEND in content:
        warn(
            f"Found non-standard build-backend '{LEGACY_BACKEND}'.\n"
            "  This backend was briefly added then removed from setuptools.\n"
            f"  Replacing with the correct '{CORRECT_BACKEND}'."
        )
        pyproject.write_text(
            content.replace(LEGACY_BACKEND, CORRECT_BACKEND), encoding="utf-8"
        )
        ok(f"pyproject.toml updated: build-backend = '{CORRECT_BACKEND}'")
    else:
        ok("build-backend looks correct — no change needed")


# ===========================================================================
# BLAST+ INSTALLER
# ===========================================================================


def _normalize_arch(arch: str) -> str:
    """Normalise platform.machine() values to the two we use as dict keys."""
    arch = arch.lower()
    if arch in ("arm64", "aarch64"):
        return "aarch64" if SYSINFO.os == "linux" or SYSINFO.is_bsd else "arm64"
    if arch in ("x86_64", "amd64", "x64"):
        return "x86_64"
    return arch


def _blast_already_installed() -> bool:
    """Return True if all required BLAST+ binaries are already on PATH or in the install dir."""
    install_dir = _blast_install_dir()
    for binary in BLAST_BINS:
        name = binary + (".exe" if SYSINFO.os == "windows" else "")
        # Check the managed install directory first, then system PATH.
        if not (install_dir / name).exists() and not shutil.which(binary):
            return False
    return True


def _blast_version_current() -> bool:
    """Return True if the installed blastn reports the expected version."""
    install_dir = _blast_install_dir()
    exe_name = "blastn" + (".exe" if SYSINFO.os == "windows" else "")
    exe = install_dir / exe_name
    if not exe.exists():
        exe_path = shutil.which("blastn")
        if not exe_path:
            return False
        exe = Path(exe_path)
    try:
        r = subprocess.run(
            [str(exe), "-version"], capture_output=True, text=True, timeout=10
        )
        # blastn -version output: "blastn: 2.17.0+\n Package: blast 2.17.0, ..."
        return BLAST_VERSION.rstrip("+") in (r.stdout + r.stderr)
    except Exception:
        return False


def install_blast(*, no_path: bool) -> None:
    """
    Download the BLAST+ tarball for the current platform, extract the six
    binaries ScriptoScope needs, install them to ~/.local/blast/bin, and
    add that directory to PATH.

    This never requires root — everything lands in the user's home directory.
    No system-wide installation is performed.
    """
    step(f"Installing BLAST+ {BLAST_VERSION}")

    # ── Check whether we need to do anything ──────────────────────────────
    if _blast_already_installed() and _blast_version_current():
        ok(f"BLAST+ {BLAST_VERSION} is already installed — nothing to do")
        _ensure_blast_on_path(no_path=no_path)
        return

    # ── Resolve the download URL for this platform/arch ───────────────────
    arch = _normalize_arch(SYSINFO.arch)
    os_key = SYSINFO.os

    tarball_name = BLAST_TARBALLS.get((os_key, arch))
    if not tarball_name:
        warn(
            f"No BLAST+ tarball available for {os_key}/{arch}.\n"
            f"  Supported combinations: {', '.join(f'{o}/{a}' for o, a in BLAST_TARBALLS)}\n"
            f"  Install BLAST+ manually from: https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST/"
        )
        return

    url = f"{_BLAST_BASE}/{tarball_name}"
    info(f"Platform   : {os_key}/{arch}")
    info(f"Tarball    : {tarball_name}")
    info(f"URL        : {url}")

    # ── Download ──────────────────────────────────────────────────────────
    import tarfile as tarfile_mod
    import tempfile
    import urllib.request

    # Show a simple progress indicator.
    _last_pct: list[int] = [-1]

    def _reporthook(block: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        pct = min(100, int(block * block_size * 100 / total))
        if pct != _last_pct[0] and pct % 10 == 0:
            info(f"  Download progress: {pct}%")
            _last_pct[0] = pct

    with tempfile.TemporaryDirectory(prefix="blast-download-") as tmpdir:
        tarball_path = Path(tmpdir) / tarball_name
        info(f"Downloading {tarball_name} (~200–300 MB, this will take a moment)...")

        try:
            # Try curl/wget first — they give better progress on terminals.
            if have("curl"):
                run(["curl", "-fL", "--progress-bar", "-o", str(tarball_path), url])
            elif have("wget"):
                run(
                    ["wget", "-q", "--show-progress", "-O", str(tarball_path), url],
                    check=False,
                )
                if not tarball_path.exists() or tarball_path.stat().st_size < 1_000_000:
                    run(["wget", "-O", str(tarball_path), url])
            else:
                # Pure Python fallback — no progress bar but always works inside the APE.
                info("  (curl/wget not found — using Python urllib, no progress bar)")
                urllib.request.urlretrieve(
                    url, str(tarball_path), reporthook=_reporthook
                )
        except Exception as exc:
            warn(f"Download failed: {exc}")
            warn(
                f"Install BLAST+ manually from: https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST/"
            )
            return

        if not tarball_path.exists() or tarball_path.stat().st_size < 1_000_000:
            warn(
                f"Downloaded file looks truncated ({tarball_path.stat().st_size if tarball_path.exists() else 0} bytes)."
            )
            warn(
                "Install BLAST+ manually from: https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST/"
            )
            return

        ok(f"Downloaded {tarball_name} ({tarball_path.stat().st_size // 1_048_576} MB)")

        # ── Extract only the binaries we need ─────────────────────────────
        info("Extracting BLAST+ binaries...")
        install_dir = _blast_install_dir()
        install_dir.mkdir(parents=True, exist_ok=True)

        suffix = ".exe" if SYSINFO.os == "windows" else ""
        extracted_count = 0
        try:
            with tarfile_mod.open(str(tarball_path), "r:gz") as tf:
                for member in tf.getmembers():
                    # The tarball has a top-level directory like ncbi-blast-2.17.0+/bin/blastn
                    basename = Path(member.name).name
                    stem = basename.replace(".exe", "")
                    if stem in BLAST_BINS and member.isfile():
                        member.name = basename  # strip directory prefix
                        tf.extract(member, path=str(install_dir))
                        dest = install_dir / basename
                        dest.chmod(0o755)
                        extracted_count += 1
                        info(f"  Installed: {dest}  ({member.size // 1_048_576} MB)")
        except Exception as exc:
            warn(f"Extraction failed: {exc}")
            warn(
                "Install BLAST+ manually from: https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/LATEST/"
            )
            return

        if extracted_count == 0:
            warn(
                "No BLAST+ binaries were found in the tarball (unexpected tarball structure)."
            )
            return
        if extracted_count < len(BLAST_BINS):
            warn(
                f"Only {extracted_count}/{len(BLAST_BINS)} BLAST+ binaries were found — tarball may be incomplete."
            )

    # ── Add to PATH ───────────────────────────────────────────────────────
    _ensure_blast_on_path(no_path=no_path)

    # ── Verify ────────────────────────────────────────────────────────────
    blastn_exe = install_dir / ("blastn" + suffix)
    if blastn_exe.exists():
        try:
            r = subprocess.run(
                [str(blastn_exe), "-version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ver_line = (r.stdout + r.stderr).split("\n")[0].strip()
            ok(f"BLAST+ installed: {ver_line}")
            ok(f"Binaries in: {install_dir}")
        except Exception as exc:
            warn(f"Installed but could not verify: {exc}")
    else:
        warn(f"Expected binary not found at {blastn_exe} — extraction may have failed.")


def _ensure_blast_on_path(*, no_path: bool) -> None:
    """Add ~/.local/blast/bin to PATH in the current session and shell profiles."""
    install_dir = _blast_install_dir()
    add_to_path(str(install_dir), no_path=no_path)

    if SYSINFO.os == "windows":
        # On Windows, also set it permanently in the user environment registry.
        try:
            import winreg  # type: ignore

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS
            )
            try:
                current_path, _ = winreg.QueryValueEx(key, "PATH")
            except FileNotFoundError:
                current_path = ""
            blast_dir_str = str(install_dir)
            if blast_dir_str not in current_path:
                new_path = (
                    f"{blast_dir_str};{current_path}" if current_path else blast_dir_str
                )
                winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
                info(
                    f"Added {blast_dir_str} to user PATH in registry (effective after re-login)"
                )
            winreg.CloseKey(key)
        except Exception:
            pass  # Non-fatal — the session PATH is already updated by add_to_path


# ===========================================================================
# PIPX INSTALL / INJECT
# ===========================================================================
def _pipx_cmd() -> list[str]:
    """Return the pipx invocation to use in the current session."""
    if have("pipx"):
        return ["pipx"]
    # Try as a module of the bundled APE Python (last resort).
    return [sys.executable, "-m", "pipx"]


def install_scriptoscope(repo: Path, system_python: str) -> None:
    step("Installing ScriptoScope into a pipx venv")
    pipx = _pipx_cmd()
    run(pipx + ["install", str(repo), "--force", "--python", system_python])
    ok("scriptoscope installed via pipx")


def inject_requirements(repo: Path) -> None:
    step("Injecting dependencies from requirements.txt")
    req = repo / "requirements.txt"
    if not req.exists():
        warn("requirements.txt not found — skipping.")
        return
    pipx = _pipx_cmd()
    run(pipx + ["inject", "scriptoscope", "-r", str(req), "--force"])
    ok("requirements.txt dependencies injected")


def inject_test_deps() -> None:
    step("Injecting test dependencies (pytest, pytest-asyncio)")
    pipx = _pipx_cmd()
    run(pipx + ["inject", "scriptoscope", "pytest", "pytest-asyncio", "--force"])
    ok("Test dependencies injected")


# ===========================================================================
# VENV HELPERS
# ===========================================================================
def venv_python() -> Path | None:
    """Return the Path to the Python inside the scriptoscope pipx venv."""
    if SYSINFO.os == "windows":
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
        p = (
            base
            / ".local"
            / "pipx"
            / "venvs"
            / "scriptoscope"
            / "Scripts"
            / "python.exe"
        )
    else:
        pipx_home = Path(os.environ.get("PIPX_HOME", Path.home() / ".local" / "pipx"))
        p = pipx_home / "venvs" / "scriptoscope" / "bin" / "python"
    return p if p.exists() else None


# ===========================================================================
# TEST RUNNER
# ===========================================================================
def run_tests(repo: Path, *, skip: bool) -> None:
    if skip:
        warn("--no-tests passed — skipping test run.")
        return

    step("Running the test suite (105 tests)")
    vpy = venv_python()
    if vpy and vpy.exists():
        run([str(vpy), "-m", "pytest", str(repo / "tests"), "-q"])
    else:
        warn(
            "Could not locate venv Python for test run.\n"
            "Activate the venv manually and run: pytest tests/ -q"
        )


# ===========================================================================
# DEVELOPER DAEMON BUILDER
# ===========================================================================


# URL and cache path for the Cosmopolitan Python base binary.
_COSMO_PYTHON_URL = "https://cosmo.zip/pub/cosmos/bin/python"
_COSMO_CACHE_DIR_NAME = "scriptoscope-ape"
_COSMO_CACHE_FILENAME = "python.com"


def _cosmo_cache_dir() -> Path:
    """Return the cache directory for the downloaded Cosmopolitan Python."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        base = Path(xdg)
    elif SYSINFO.os == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path.home() / ".cache"
    return base / _COSMO_CACHE_DIR_NAME


def _download_cosmo_python(dest: Path) -> bool:
    """
    Download the Cosmopolitan Python binary to *dest*.

    Tries curl first (best progress display), then wget, then falls back to
    Python's own urllib (always available inside the APE — no external tools
    required).  Returns True on success.
    """
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)

    if have("curl"):
        info("Downloading Cosmopolitan Python via curl …")
        try:
            run(["curl", "-fL", "--progress-bar", "-o", str(dest), _COSMO_PYTHON_URL])
            return dest.exists() and dest.stat().st_size > 1_000_000
        except subprocess.CalledProcessError:
            warn("curl download failed — trying urllib fallback.")

    if have("wget"):
        info("Downloading Cosmopolitan Python via wget …")
        try:
            run(
                ["wget", "-q", "--show-progress", "-O", str(dest), _COSMO_PYTHON_URL],
                check=False,
            )
            if dest.exists() and dest.stat().st_size > 1_000_000:
                return True
            run(["wget", "-O", str(dest), _COSMO_PYTHON_URL])
            return dest.exists() and dest.stat().st_size > 1_000_000
        except subprocess.CalledProcessError:
            warn("wget download failed — trying urllib fallback.")

    # Pure-stdlib fallback — always available inside the APE binary, no
    # external tools needed.  No fancy progress bar, but it works everywhere.
    info("Downloading Cosmopolitan Python via urllib (no external tools needed) …")
    info(f"  URL: {_COSMO_PYTHON_URL}")
    info("  This is ~35 MB — please wait …")
    try:
        urllib.request.urlretrieve(_COSMO_PYTHON_URL, str(dest))
        return dest.exists() and dest.stat().st_size > 1_000_000
    except Exception as exc:
        warn(f"urllib download failed: {exc}")
        return False


def _ensure_cosmo_python() -> Path | None:
    """
    Return the path to a cached Cosmopolitan Python binary, downloading it
    if necessary.  Returns None on failure.
    """
    cache_dir = _cosmo_cache_dir()
    cached = cache_dir / _COSMO_CACHE_FILENAME

    if cached.exists() and cached.stat().st_size > 1_000_000:
        ok(
            f"Using cached Cosmopolitan Python ({cached.stat().st_size // 1_048_576} MB)"
        )
        return cached

    info("Cosmopolitan Python not cached — downloading …")
    if _download_cosmo_python(cached):
        ok(f"Downloaded Cosmopolitan Python ({cached.stat().st_size // 1_048_576} MB)")
        return cached

    warn("Failed to download Cosmopolitan Python.")
    return None


def build_developer_daemon(repo: Path) -> bool:
    """
    Build developer-daemon.com using pure Python stdlib.

    No sh, no curl, no wget, no zip binary required — uses urllib.request for
    downloading and zipfile for injection.  External download tools (curl/wget)
    are tried first for better progress display but the stdlib fallback always
    works.

    Returns True on success, False on a soft failure so the caller can warn
    without aborting the overall setup.
    """
    import zipfile as zf

    step("Building developer-daemon.com")

    # ── Locate source files ──────────────────────────────────────────────
    watch_py = repo / "devtools" / "watch.py"
    sitecustomize_py = repo / "devtools" / "sitecustomize.py"
    output = repo / "developer-daemon.com"

    if not watch_py.exists():
        warn(f"devtools/watch.py not found at {watch_py}.")
        return False
    if not sitecustomize_py.exists():
        warn(f"devtools/sitecustomize.py not found at {sitecustomize_py}.")
        return False

    # ── Get the Cosmopolitan Python base binary ──────────────────────────
    cosmo_python = _ensure_cosmo_python()
    if cosmo_python is None:
        warn(
            "Cannot build developer-daemon.com without the Cosmopolitan Python binary.\n"
            "  Check your internet connection and try again."
        )
        return False

    # ── Copy the base binary to the output path ──────────────────────────
    info(f"Copying Cosmopolitan Python → {output.name} …")
    try:
        shutil.copy2(str(cosmo_python), str(output))
        output.chmod(0o755)
    except OSError as exc:
        warn(f"Failed to copy Cosmopolitan Python: {exc}")
        return False

    # ── Inject watch.py as __main__.py and sitecustomize.py as
    #    Lib/sitecustomize.py into the binary's ZIP layer ─────────────────
    info("Injecting devtools/watch.py as __main__.py …")
    info("Injecting devtools/sitecustomize.py as Lib/sitecustomize.py …")
    try:
        with zf.ZipFile(str(output), "a", compression=zf.ZIP_DEFLATED) as z:
            z.write(str(watch_py), "__main__.py")
            z.write(str(sitecustomize_py), "Lib/sitecustomize.py")
    except Exception as exc:
        warn(f"Failed to inject files into {output.name}: {exc}")
        # Clean up the broken output so it doesn't confuse anyone.
        output.unlink(missing_ok=True)
        return False

    # ── Verify ───────────────────────────────────────────────────────────
    try:
        with zf.ZipFile(str(output), "r") as z:
            names = z.namelist()
            if "__main__.py" not in names:
                warn("__main__.py not found in assembled binary.")
                return False
            if "Lib/sitecustomize.py" not in names:
                warn("Lib/sitecustomize.py not found in assembled binary.")
                return False
    except Exception as exc:
        warn(f"ZIP verification failed: {exc}")
        return False

    ok(f"developer-daemon.com built ({output.stat().st_size // 1_048_576} MB)")
    return True


# ===========================================================================
# SUMMARY
# ===========================================================================
def print_summary(
    repo: Path,
    system_python: str,
    blast_installed: bool = False,
    daemon_built: bool = False,
) -> None:
    vpy = venv_python()
    venv = vpy.parent.parent if vpy else Path("~/.local/pipx/venvs/scriptoscope")
    blast_dir = _blast_install_dir()

    if SYSINFO.os == "windows":
        activate = str(venv / "Scripts" / "Activate.ps1")
        run_cmd = "scriptoscope"
        pytest_cmd = str(vpy or "python") + " -m pytest tests\\ -q"
        blast_status = (
            f"{blast_dir}"
            if blast_installed
            else "(not installed — run without --no-blast)"
        )
    else:
        activate = f"source {venv / 'bin' / 'activate'}"
        run_cmd = "scriptoscope"
        pytest_cmd = f"{vpy or 'python3'} -m pytest {repo / 'tests'} -q"
        blast_status = (
            f"{blast_dir}"
            if blast_installed
            else "(not installed — run without --no-blast)"
        )

    header("Setup Complete!")
    lines = [
        f"{_B}ScriptoScope 0.6.0 is ready.{_R}",
        "",
        f"{_B}Run the app:{_R}",
        f"  {_GR}{run_cmd}{_R} /path/to/transcriptome.fasta",
        f"  {_GR}{run_cmd}{_R}   (open a file from within the UI)",
        "",
        f"{_B}Activate the pipx venv (for development):{_R}",
        f"  {_CY}{activate}{_R}",
        "",
        f"{_B}Run the tests:{_R}",
        f"  {_CY}{pytest_cmd}{_R}",
        "",
        f"{_B}BLAST+ binaries:{_R}",
        f"  {_CY}{blast_status}{_R}",
        f"  (blastn, blastp, blastx, tblastn, tblastx, makeblastdb)",
        "",
        f"{_B}Developer daemon:{_R}",
        f"  {_CY}{str(repo / 'developer-daemon.com')}{_R}"
        if daemon_built
        else f"  {_YL}(not built — run: sh devtools/build-ape.sh){_R}",
        f"  Start with: ./developer-daemon.com",
        "",
        f"{_B}Update after git pull:{_R}",
        f"  pipx install {repo} --force",
        f"  pipx inject scriptoscope -r {repo / 'requirements.txt'} --force",
        "",
        f"{_B}Uninstall:{_R}",
        f"  pipx uninstall scriptoscope",
        "",
        f"{_B}Log file:{_R}  /tmp/scriptoscope.log (override: SCRIPTOSCOPE_LOG=...)",
        "",
        f"See {_CY}DEVELOPERS.md{_R} for full documentation.",
    ]
    for line in lines:
        print(f"  {line}")
    print()


# ===========================================================================
# MAIN
# ===========================================================================
def main() -> None:
    args = parse_args()

    header("ScriptoScope Developer Environment Bootstrap")
    print(f"  {_B}Platform{_R}         : {SYSINFO}")
    print(f"  {_B}Architecture{_R}     : {SYSINFO.arch}")
    print(f"  {_B}APE Python{_R}       : {sys.version.split()[0]}  ({sys.executable})")
    print(f"  {_B}Target Python{_R}    : {args.version}+")
    print(
        f"  {_B}BLAST+{_R}           : {'skipped (--no-blast)' if args.no_blast else BLAST_VERSION}"
    )
    print(
        f"  {_B}Daemon build{_R}     : {'skipped (--no-daemon)' if args.no_daemon else 'developer-daemon.com'}"
    )
    print()

    # ── --blast-only: just install BLAST+ and exit ───────────────────────────
    if args.blast_only:
        install_blast(no_path=args.no_path)
        ok("--blast-only: done.")
        return

    # ── Step 1: Install system Python ───────────────────────────────────────
    system_python = ensure_system_python(args)

    if args.python_only:
        ok("--python-only: stopping after system Python installation.")
        return

    # ── Step 2: Locate repo ──────────────────────────────────────────────────
    repo = find_repo_root()
    info(f"Repository root: {repo}")

    # ── Step 3: Fix pyproject.toml ───────────────────────────────────────────
    fix_build_backend(repo)

    # ── Step 4: Install pipx ─────────────────────────────────────────────────
    ensure_pipx(system_python, no_path=args.no_path)

    # ── Step 5: Install ScriptoScope via pipx ────────────────────────────────
    install_scriptoscope(repo, system_python)

    # ── Step 6: Inject requirements.txt ──────────────────────────────────────
    inject_requirements(repo)

    # ── Step 7: Inject test deps ──────────────────────────────────────────────
    inject_test_deps()

    # ── Step 8: Install BLAST+ ───────────────────────────────────────────────
    blast_installed = False
    if not args.no_blast:
        install_blast(no_path=args.no_path)
        blast_installed = _blast_already_installed()
    else:
        warn(
            "Skipping BLAST+ installation (--no-blast). Local BLAST searches will be unavailable."
        )

    # ── Step 9: Run tests ────────────────────────────────────────────────────
    run_tests(repo, skip=args.no_tests)

    # ── Step 10: Build developer-daemon.com ──────────────────────────────────
    daemon_built = False
    if not args.no_daemon:
        daemon_built = build_developer_daemon(repo)
    else:
        warn(
            "Skipping developer-daemon.com build (--no-daemon).\n"
            "  Build manually later with:  sh devtools/build-ape.sh"
        )

    # ── Done ──────────────────────────────────────────────────────────────────
    print_summary(
        repo, system_python, blast_installed=blast_installed, daemon_built=daemon_built
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("Bootstrap interrupted by user.")
        sys.exit(130)
    except subprocess.CalledProcessError as exc:
        err(f"Command failed with exit code {exc.returncode}.")
        err("Check the output above for details, or consult DEVELOPERS.md.")
        sys.exit(exc.returncode)
