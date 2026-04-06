#!/bin/sh
# =============================================================================
# devtools/bench-orf-compare.sh — Run the ORF engine comparison benchmark
# =============================================================================
#
# Runs devtools/bench_orf_compare.py under the project venv.
# All arguments are forwarded to the Python script.
#
# Usage (from repo root or devtools/):
#   devtools/bench-orf-compare.sh [OPTIONS]
#
# Options (passed through to bench_orf_compare.py):
#   --fasta PATH   Use a real FASTA file instead of random sequences
#   --count N      Number of random sequences (default: 100000)
#   --max-len N    Max nucleotide length    (default: 35000)
#   --min-len N    Min nucleotide length    (default: 200)
#   --rounds N     Timed rounds per engine  (default: 3)
#   --threads N    Threads for batch scan   (default: 0 = auto)
#   --validate     Cross-validate results between engines
#
# =============================================================================

set -e

# Resolve the repo root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON="$REPO_ROOT/.venv/bin/python"
SCRIPT="$REPO_ROOT/devtools/bench_orf_compare.py"

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: venv Python not found at $PYTHON" >&2
    echo "       Run setup-dev-env.com first to create the venv." >&2
    exit 1
fi

exec "$PYTHON" "$SCRIPT" "$@"
