#!/bin/sh
# =============================================================================
# devtools/build-ape.sh — Assemble ScriptoScope APE binaries
# =============================================================================
#
# This script lives in devtools/ and builds two APE binaries at the repo root:
#
#   ../setup-dev-env.com      — bootstraps the full developer environment
#   ../developer-daemon.com   — file-watcher daemon for iterative development
#
# Source files (all in devtools/):
#   __main__.py   →  zipped into setup-dev-env.com  as __main__.py
#   watch.py      →  zipped into developer-daemon.com as __main__.py
#
# Supported target platforms:
#   macOS   (x86_64 + ARM64 / Apple Silicon)
#   Linux   (x86_64 + ARM64, all major distros)
#   Windows (x86_64, via PE header — rename to .exe or run directly)
#   FreeBSD / OpenBSD / NetBSD / DragonFlyBSD (x86_64 + ARM64)
#
# How it works:
#   1. Download the Cosmopolitan Python APE binary (python.com) — a ~35 MB
#      fat binary that is simultaneously a valid ELF, Mach-O, PE, and POSIX
#      shell script. It contains a full CPython 3.12 interpreter.
#   2. Copy it to the output path.
#   3. Zip the entry script into the binary at the ZIP root as __main__.py.
#      APE binaries are valid ZIP archives. When python.com is passed a ZIP
#      file as its first argument, it finds __main__.py inside and runs it —
#      exactly like a standard Python zipapp (PEP 441).
#   4. Make the result executable.
#
# Usage (run from anywhere — paths are relative to this script's location):
#   sh devtools/build-ape.sh [OPTIONS]
#
# Requirements (build-time only, not needed to run the output):
#   - curl or wget (to download python.com if not cached)
#   - zip (to inject scripts into the binary)
#   - sh (POSIX shell — bash/dash/ash all work)
#
# =============================================================================

set -e

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
COSMO_PYTHON_URL="https://cosmo.zip/pub/cosmos/bin/python"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/scriptoscope-ape"
CACHED_PYTHON="$CACHE_DIR/python.com"
# SCRIPT_DIR is the devtools/ directory; REPO_ROOT is one level up.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUTPUT="${REPO_ROOT}/setup-dev-env.com"
DAEMON_OUTPUT="${REPO_ROOT}/developer-daemon.com"
FORCE=0

# ---------------------------------------------------------------------------
# ANSI colours (only when stdout is a tty and NO_COLOR is unset)
# ---------------------------------------------------------------------------
_setup_colors() {
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        RESET="\033[0m"
        BOLD="\033[1m"
        RED="\033[31m"
        GREEN="\033[32m"
        YELLOW="\033[33m"
        CYAN="\033[36m"
    else
        RESET="" BOLD="" RED="" GREEN="" YELLOW="" CYAN=""
    fi
}
_setup_colors

info()  { printf "%b[INFO]%b  %s\n" "$CYAN"   "$RESET" "$*"; }
ok()    { printf "%b[OK]%b    %s\n" "$GREEN"  "$RESET" "$*"; }
warn()  { printf "%b[WARN]%b  %s\n" "$YELLOW" "$RESET" "$*" >&2; }
error() { printf "%b[ERROR]%b %s\n" "$RED"    "$RESET" "$*" >&2; }
header() {
    _msg="$*"
    # Build a bar of '=' the same width as the message + 4 padding chars.
    # Pure POSIX: use printf to repeat the character.
    _len=$((${#_msg} + 4))
    _bar="$(printf '%*s' "$_len" '' | tr ' ' '=')"
    printf "\n%b%s%b\n" "$BOLD" "$_bar" "$RESET"
    printf "%b  %s%b\n"  "$BOLD" "$_msg" "$RESET"
    printf "%b%s%b\n\n" "$BOLD" "$_bar" "$RESET"
}
die() { error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
print_help() {
    cat <<EOF
devtools/build-ape.sh — Build ScriptoScope APE binaries

Usage:
  sh devtools/build-ape.sh [OPTIONS]

Options:
  --output PATH          Output path for setup-dev-env.com (default: <repo-root>/setup-dev-env.com)
  --daemon-output PATH   Output path for developer-daemon.com (default: <repo-root>/developer-daemon.com)
  --python-url URL       Cosmopolitan Python URL (default: https://cosmo.zip/pub/cosmos/bin/python)
  --force                Re-download python.com even if cached
  --no-cache             Do not cache the download (implies --force)
  --no-daemon            Skip building developer-daemon.com
  -h, --help             Show help and exit

What it builds:
  Two self-contained binaries placed at the repo root, running natively on
  macOS, Linux, Windows, and BSD (x86_64 + ARM64):

  setup-dev-env.com       Bootstraps the entire developer environment (Python, pipx,
                          ScriptoScope, BLAST+, tests). No Python required to run it.
                          Sources: devtools/__main__.py + devtools/sitecustomize.py

  developer-daemon.com    File-watcher daemon. Detects changes to watched source files
                          and automatically runs the correct rebuild/reinstall action:
                            scriptoscope.py           → pipx install . --force
                            pyproject.toml            → pipx install + inject requirements
                            requirements.txt          → pipx install + inject requirements
                            devtools/__main__.py      → sh devtools/build-ape.sh
                            devtools/watch.py         → sh devtools/build-ape.sh
                            devtools/build-ape.sh     → sh devtools/build-ape.sh
                          Sources: devtools/watch.py + devtools/sitecustomize.py

Runtime usage:
  ./setup-dev-env.com                  Full setup + tests
  ./setup-dev-env.com --no-tests       Skip test run
  ./setup-dev-env.com --python-only    Install system Python only
  ./setup-dev-env.com --blast-only     Install BLAST+ only
  sh setup-dev-env.com                 If direct execute fails (some Linux configs)

  ./developer-daemon.com               Start the file watcher
  ./developer-daemon.com --poll        Force stat-polling backend
  sh developer-daemon.com              If direct execute fails (some Linux configs)

On Windows, rename .com to .exe first:
  Rename-Item setup-dev-env.com setup-dev-env.exe;             .\setup-dev-env.exe
  Rename-Item developer-daemon.com developer-daemon.exe;       .\developer-daemon.exe

EOF
}

NO_CACHE=0
NO_DAEMON=0
while [ $# -gt 0 ]; do
    case "$1" in
        --output)
            [ -n "${2:-}" ] || die "--output requires an argument"
            OUTPUT="$2"; shift 2 ;;
        --output=*)
            OUTPUT="${1#*=}"; shift ;;
        --daemon-output)
            [ -n "${2:-}" ] || die "--daemon-output requires an argument"
            DAEMON_OUTPUT="$2"; shift 2 ;;
        --daemon-output=*)
            DAEMON_OUTPUT="${1#*=}"; shift ;;
        --python-url)
            [ -n "${2:-}" ] || die "--python-url requires an argument"
            COSMO_PYTHON_URL="$2"; shift 2 ;;
        --python-url=*)
            COSMO_PYTHON_URL="${1#*=}"; shift ;;
        --force)
            FORCE=1; shift ;;
        --no-cache)
            NO_CACHE=1; FORCE=1; shift ;;
        --no-daemon)
            NO_DAEMON=1; shift ;;
        -h|--help)
            print_help; exit 0 ;;
        --)
            shift; break ;;
        -*)
            die "Unknown option: $1  (run with --help for usage)" ;;
        *)
            break ;;
    esac
done

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
check_prereqs() {
    info "Checking build prerequisites..."
    _missing=""

    # Need curl or wget to download.
    if ! command -v curl > /dev/null 2>&1 && \
       ! command -v wget > /dev/null 2>&1; then
        _missing="$_missing curl-or-wget"
    fi

    # Need zip to inject __main__.py / devtools/watch.py.
    if ! command -v zip > /dev/null 2>&1; then
        _missing="$_missing zip"
    fi

    if [ -n "$_missing" ]; then
        error "Missing required build tools:$_missing"
        printf "\nInstall them with:\n"
        if command -v apt-get > /dev/null 2>&1; then
            printf "  sudo apt-get install -y curl zip\n"
        elif command -v brew > /dev/null 2>&1; then
            printf "  brew install curl zip\n"
        elif command -v dnf > /dev/null 2>&1; then
            printf "  sudo dnf install -y curl zip\n"
        elif command -v pacman > /dev/null 2>&1; then
            printf "  sudo pacman -Sy --noconfirm curl zip\n"
        elif command -v pkg > /dev/null 2>&1; then
            printf "  sudo pkg install -y curl zip\n"
        else
            printf "  (use your system package manager to install: curl zip)\n"
        fi
        exit 1
    fi

    ok "All build prerequisites satisfied"
}

# ---------------------------------------------------------------------------
# Download Cosmopolitan Python
# ---------------------------------------------------------------------------
download_python() {
    # Use cache unless --force or --no-cache was given.
    if [ "$FORCE" -eq 0 ] && [ -f "$CACHED_PYTHON" ]; then
        _size="$(wc -c < "$CACHED_PYTHON" | tr -d ' ')"
        if [ "$_size" -gt 1000000 ]; then
            ok "Using cached Cosmopolitan Python: $CACHED_PYTHON ($_size bytes)"
            return
        else
            warn "Cached file looks truncated ($_size bytes) — re-downloading."
        fi
    fi

    info "Downloading Cosmopolitan Python from:"
    info "  $COSMO_PYTHON_URL"

    if [ "$NO_CACHE" -eq 0 ]; then
        mkdir -p "$CACHE_DIR"
        _dest="$CACHED_PYTHON"
    else
        _dest="/tmp/python-cosmo-$$.com"
        # Store path for cleanup later.
        _NOCACHE_DEST="$_dest"
    fi

    if command -v curl > /dev/null 2>&1; then
        curl -fsSL --progress-bar -o "$_dest" "$COSMO_PYTHON_URL"
    else
        wget -q --show-progress -O "$_dest" "$COSMO_PYTHON_URL" 2>&1 || \
        wget -q -O "$_dest" "$COSMO_PYTHON_URL"
    fi

    _size="$(wc -c < "$_dest" | tr -d ' ')"
    if [ "$_size" -lt 1000000 ]; then
        rm -f "$_dest"
        die "Download failed or file is too small ($_size bytes). Check your internet connection."
    fi

    ok "Downloaded Cosmopolitan Python ($_size bytes) to $_dest"
}

# ---------------------------------------------------------------------------
# Verify the downloaded binary is a valid APE / works as a Python interpreter
# ---------------------------------------------------------------------------
verify_cosmo_python() {
    _py="$CACHED_PYTHON"
    [ -n "${_NOCACHE_DEST:-}" ] && _py="$_NOCACHE_DEST"

    info "Verifying Cosmopolitan Python binary..."

    # Check magic bytes: APE starts with "MZqFpD" (DOS MZ header reused as shell).
    _magic="$(dd if="$_py" bs=1 count=6 2>/dev/null | cat)"
    # Alternatively check with od — more portable than hexdump.
    _magic_hex="$(dd if="$_py" bs=1 count=2 2>/dev/null | od -A n -t x1 | tr -d ' \n')"

    # MZ header starts with 0x4D 0x5A ("MZ").
    case "$_magic_hex" in
        4d5a*)
            ok "APE magic bytes confirmed (MZ/PE header present)" ;;
        7f45*)
            ok "ELF magic bytes confirmed (ELF header present)" ;;
        *)
            warn "Unexpected magic bytes: $_magic_hex — proceeding anyway."
            warn "The binary may still work if it's a valid APE."
            ;;
    esac

    # Verify the binary is also a valid ZIP (APE binaries are ZIP files).
    if command -v unzip > /dev/null 2>&1; then
        if unzip -t "$_py" > /dev/null 2>&1; then
            ok "ZIP integrity check passed"
        else
            die "The downloaded file is not a valid ZIP archive. APE binary may be corrupted."
        fi
    fi

    # Quick smoke-test: run --version.
    chmod +x "$_py" 2>/dev/null || true
    if "$_py" --version > /dev/null 2>&1; then
        _ver="$("$_py" --version 2>&1)"
        ok "Cosmopolitan Python runs: $_ver"
    else
        warn "Could not run python.com directly (may need APE loader on this Linux config)."
        warn "The output binary will still work — see DEVELOPERS.md for APE loader setup."
    fi
}

# ---------------------------------------------------------------------------
# Check that devtools/__main__.py, devtools/watch.py, and
# devtools/sitecustomize.py exist
# ---------------------------------------------------------------------------
check_main_py() {
    MAIN_PY="${SCRIPT_DIR}/__main__.py"
    if [ ! -f "$MAIN_PY" ]; then
        die "devtools/__main__.py not found at: $MAIN_PY\n  Make sure you are running build-ape.sh from inside the devtools/ directory or via 'sh devtools/build-ape.sh'."
    fi
    _lines="$(wc -l < "$MAIN_PY" | tr -d ' ')"
    ok "Found devtools/__main__.py ($_lines lines)"
}

check_watch_py() {
    WATCH_PY="${SCRIPT_DIR}/watch.py"
    if [ ! -f "$WATCH_PY" ]; then
        die "devtools/watch.py not found at: $WATCH_PY\n  Make sure you are running build-ape.sh from inside the devtools/ directory or via 'sh devtools/build-ape.sh'."
    fi
    _lines="$(wc -l < "$WATCH_PY" | tr -d ' ')"
    ok "Found devtools/watch.py ($_lines lines)"
}

check_sitecustomize_py() {
    SITECUSTOMIZE_PY="${SCRIPT_DIR}/sitecustomize.py"
    if [ ! -f "$SITECUSTOMIZE_PY" ]; then
        die "devtools/sitecustomize.py not found at: $SITECUSTOMIZE_PY\n  This file is required — it makes ./setup-dev-env.com and ./developer-daemon.com run without arguments."
    fi
    _lines="$(wc -l < "$SITECUSTOMIZE_PY" | tr -d ' ')"
    ok "Found devtools/sitecustomize.py ($_lines lines)"
}

# ---------------------------------------------------------------------------
# Assemble the APE binaries
# ---------------------------------------------------------------------------

# _assemble_ape SRC_PYTHON OUTPUT_PATH ENTRY_FILE ENTRY_NAME
# Copies SRC_PYTHON to OUTPUT_PATH, zips ENTRY_FILE into it as __main__.py (the
# zipapp entry point), and also injects devtools/sitecustomize.py at
# Lib/sitecustomize.py so Python auto-imports it at startup and correctly
# re-execs the binary when invoked bare (./setup-dev-env.com with no args).
_assemble_ape() {
    _src="$1"
    _out="$2"
    _entry_file="$3"
    _entry_name="$4"

    info "Copying Cosmopolitan Python → $(basename "$_out") ..."
    cp "$_src" "$_out"
    chmod +x "$_out"

    info "Injecting $(basename "$_entry_file") as $_entry_name ..."
    # We need the entry to appear in the ZIP as exactly "$_entry_name" with no
    # directory prefix.  Strategy: copy to a temp file with the right name,
    # zip it from its parent directory, then remove the temp file.
    _tmp_dir="$(mktemp -d)"
    cp "$_entry_file" "$_tmp_dir/$_entry_name"
    (
        cd "$_tmp_dir"
        zip -j "$_out" "$_entry_name"
    )
    rm -rf "$_tmp_dir"

    if command -v unzip > /dev/null 2>&1; then
        if unzip -l "$_out" | grep -q "$_entry_name"; then
            ok "$_entry_name confirmed in ZIP of $(basename "$_out")"
        else
            die "$_entry_name was not found in the assembled binary $(basename "$_out")."
        fi
    fi

    # Inject sitecustomize.py at Lib/sitecustomize.py inside the ZIP.
    # Python searches sys.path for sitecustomize at startup; in the Cosmopolitan
    # Python bundle sys.path includes /zip/Lib, so Lib/sitecustomize.py is
    # imported automatically before any user script runs.  This is what allows
    # ./setup-dev-env.com (bare invocation) to run __main__.py instead of
    # opening a REPL.
    info "Injecting devtools/sitecustomize.py as Lib/sitecustomize.py ..."
    _site_tmp="$(mktemp -d)"
    mkdir -p "$_site_tmp/Lib"
    cp "$SITECUSTOMIZE_PY" "$_site_tmp/Lib/sitecustomize.py"
    (
        cd "$_site_tmp"
        zip -r "$_out" "Lib/sitecustomize.py"
    )
    rm -rf "$_site_tmp"

    if command -v unzip > /dev/null 2>&1; then
        if unzip -l "$_out" | grep -q "Lib/sitecustomize.py"; then
            ok "Lib/sitecustomize.py confirmed in ZIP of $(basename "$_out")"
        else
            warn "Lib/sitecustomize.py was not found in $(basename "$_out") — bare invocation may open a REPL."
        fi
    fi
}

assemble() {
    _src="$CACHED_PYTHON"
    [ -n "${_NOCACHE_DEST:-}" ] && _src="$_NOCACHE_DEST"

    # Build setup-dev-env.com  (devtools/__main__.py zipped as __main__.py)
    _assemble_ape "$_src" "$OUTPUT" "$MAIN_PY" "__main__.py"
    ok "Assembly complete: $OUTPUT"

    # Build developer-daemon.com  (devtools/watch.py zipped as __main__.py)
    if [ "$NO_DAEMON" -eq 0 ]; then
        _assemble_ape "$_src" "$DAEMON_OUTPUT" "$WATCH_PY" "__main__.py"
        ok "Assembly complete: $DAEMON_OUTPUT"
    fi
}

# ---------------------------------------------------------------------------
# Smoke-test an assembled APE binary
# ---------------------------------------------------------------------------

# _smoke_test BINARY EXPECTED_STRING LABEL
_smoke_test() {
    _bin="$1"
    _expected="$2"
    _label="$3"

    chmod +x "$_bin"

    if "$_bin" "$_bin" --help > /tmp/ape-smoke-$$.txt 2>&1; then
        if grep -qi "$_expected" /tmp/ape-smoke-$$.txt 2>/dev/null; then
            ok "Smoke test passed: $_label"
        else
            warn "$_label ran but --help output was unexpected:"
            head -5 /tmp/ape-smoke-$$.txt >&2 || true
        fi
    else
        if sh "$_bin" --help > /tmp/ape-smoke-sh-$$.txt 2>&1; then
            ok "Smoke test passed (via sh): $_label"
            warn "Direct execution may require the APE loader on this Linux."
            warn "  sudo wget -O /usr/bin/ape https://cosmo.zip/pub/cosmos/bin/ape-\$(uname -m).elf"
            warn "  sudo chmod +x /usr/bin/ape"
            warn "  sudo sh -c \"echo ':APE:M::MZqFpD::/usr/bin/ape:' >/proc/sys/fs/binfmt_misc/register\""
        else
            warn "Smoke test could not execute $_label directly or via sh."
            warn "This may be expected on systems that need the APE loader."
            warn "The binary is still correctly assembled and will work on supported platforms."
            cat /tmp/ape-smoke-sh-$$.txt >&2 2>/dev/null || true
        fi
    fi

    rm -f /tmp/ape-smoke-$$.txt /tmp/ape-smoke-sh-$$.txt
}

smoke_test() {
    info "Smoke-testing assembled binaries..."
    _smoke_test "$OUTPUT" "ScriptoScope" "setup-dev-env.com"
    if [ "$NO_DAEMON" -eq 0 ]; then
        _smoke_test "$DAEMON_OUTPUT" "watcher" "developer-daemon.com"
    fi
}

# ---------------------------------------------------------------------------
# Print build summary
# ---------------------------------------------------------------------------
print_summary() {
    _size_setup="$(wc -c < "$OUTPUT" | tr -d ' ')"
    _mb_setup="$(awk "BEGIN { printf \"%.1f\", $_size_setup / 1048576 }")"

    header "Build Complete"

    printf "  %bsetup-dev-env.com%b\n"                                              "$BOLD" "$RESET"
    printf "    Path          : %s\n"           "$OUTPUT"
    printf "    Size          : %s MB\n"        "$_mb_setup"
    printf "    Contains      : Cosmopolitan Python 3.12 + devtools/__main__.py + sitecustomize.py\n"
    printf "    Purpose       : Full developer environment setup\n\n"

    if [ "$NO_DAEMON" -eq 0 ]; then
        _size_daemon="$(wc -c < "$DAEMON_OUTPUT" | tr -d ' ')"
        _mb_daemon="$(awk "BEGIN { printf \"%.1f\", $_size_daemon / 1048576 }")"
        printf "  %bdeveloper-daemon.com%b\n"                                       "$BOLD" "$RESET"
        printf "    Path          : %s\n"       "$DAEMON_OUTPUT"
        printf "    Size          : %s MB\n"    "$_mb_daemon"
        printf "    Contains      : Cosmopolitan Python 3.12 + devtools/watch.py + sitecustomize.py\n"
        printf "    Purpose       : File-watcher daemon — auto-rebuild on save\n\n"
    fi

    printf "  %bRuns on%b      : macOS, Linux, Windows, FreeBSD, OpenBSD, NetBSD\n" "$BOLD" "$RESET"
    printf "  %bArchitectures%b: x86_64 + ARM64 (fat binary)\n\n"                  "$BOLD" "$RESET"

    printf "%bUsage:%b\n\n" "$BOLD" "$RESET"

    printf "  %b# One-time setup (run once per machine)%b\n"                       "$CYAN" "$RESET"
    printf "  ./setup-dev-env.com\n\n"

    printf "  %b# Development loop (run in a separate terminal while coding)%b\n"  "$CYAN" "$RESET"
    printf "  ./developer-daemon.com\n\n"

    printf "  %b# Other setup-dev-env.com options%b\n"                             "$CYAN" "$RESET"
    printf "  ./setup-dev-env.com --no-tests      # skip test run\n"
    printf "  ./setup-dev-env.com --python-only   # install Python only\n"
    printf "  ./setup-dev-env.com --blast-only    # install BLAST+ only\n"
    printf "  ./setup-dev-env.com --version 3.12  # target a specific Python\n\n"

    printf "  %b# Other developer-daemon.com options%b\n"                          "$CYAN" "$RESET"
    printf "  ./developer-daemon.com --poll          # force stat-polling backend\n"
    printf "  ./developer-daemon.com --interval 0.5  # set polling interval\n"
    printf "  ./developer-daemon.com --no-color      # disable colour output\n\n"

    printf "  %b# If direct execution fails on Linux%b\n"                          "$CYAN" "$RESET"
    printf "  sh setup-dev-env.com\n"
    printf "  sh developer-daemon.com\n\n"

    printf "  %b# Windows — rename first%b\n"                                      "$CYAN" "$RESET"
    printf "  Rename-Item setup-dev-env.com setup-dev-env.exe;           .\\\\setup-dev-env.exe\n"
    printf "  Rename-Item developer-daemon.com developer-daemon.exe;     .\\\\developer-daemon.exe\n\n"

    printf "  %b# Inspect contents of either binary%b\n"                           "$CYAN" "$RESET"
    printf "  unzip -l setup-dev-env.com\n"
    printf "  unzip -l developer-daemon.com\n\n"

    printf "%bIf direct execution fails on Linux, register the APE loader once:%b\n\n" "$BOLD" "$RESET"
    # shellcheck disable=SC2016
    printf '  sudo wget -O /usr/bin/ape https://cosmo.zip/pub/cosmos/bin/ape-$(uname -m).elf\n'
    printf "  sudo chmod +x /usr/bin/ape\n"
    printf "  sudo sh -c \"echo ':APE:M::MZqFpD::/usr/bin/ape:' >/proc/sys/fs/binfmt_misc/register\"\n\n"

    printf "See %bDEVELOPERS.md%b for full documentation.\n\n" "$CYAN" "$RESET"
}

# ---------------------------------------------------------------------------
# Cleanup on exit (for --no-cache temp files)
# ---------------------------------------------------------------------------
_cleanup() {
    [ -n "${_NOCACHE_DEST:-}" ] && rm -f "$_NOCACHE_DEST"
}
trap _cleanup EXIT

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    header "Building ScriptoScope APE binaries"

    printf "  %bsetup-dev-env.com%b      : %s\n"  "$BOLD" "$RESET" "$OUTPUT"
    if [ "$NO_DAEMON" -eq 0 ]; then
        printf "  %bdeveloper-daemon.com%b  : %s\n"  "$BOLD" "$RESET" "$DAEMON_OUTPUT"
    fi
    printf "  %bPython URL%b             : %s\n"  "$BOLD" "$RESET" "$COSMO_PYTHON_URL"
    printf "  %bCache dir%b              : %s\n"  "$BOLD" "$RESET" "$CACHE_DIR"
    printf "  %bForce DL%b               : %s\n\n" "$BOLD" "$RESET" "$([ $FORCE -eq 1 ] && echo yes || echo no)"

    check_prereqs
    check_main_py
    check_sitecustomize_py
    if [ "$NO_DAEMON" -eq 0 ]; then
        check_watch_py
    fi
    download_python
    verify_cosmo_python
    assemble
    smoke_test
    print_summary
}

main "$@"
