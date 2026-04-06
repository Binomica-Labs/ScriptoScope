#!/usr/bin/env python3
# Run with: .venv/bin/python devtools/bench_orf.py [--watch]
"""Benchmark harness for _find_longest_orf.

Usage:
    python devtools/bench_orf.py [OPTIONS] [--watch]

Options:
    --count N      Number of sequences (default: 100000)
    --max-len N    Max nucleotide length (default: 35000)
    --min-len N    Min nucleotide length (default: 200)
    --rounds N     Timed rounds (default: 3)
    --watch        Re-run on scriptoscope.py changes

Without --watch: runs the benchmark once and prints results.
With --watch:    re-runs automatically when scriptoscope.py changes,
                 showing delta vs the original run and the previous run.
"""
from __future__ import annotations

import importlib
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Sequence generation (deterministic)
# ---------------------------------------------------------------------------

def _generate_sequences(
    seed: int = 42,
    count: int = 100_000,
    min_len: int = 200,
    max_len: int = 35_000,
) -> list[tuple[str, str]]:
    """Return (seq_id, nucleotide) pairs with a realistic length distribution.

    Real transcriptomes (e.g. Trinity de novo assemblies) typically have
    100k–300k transcripts with lengths ranging from ~200 to ~35,000 nt,
    heavily skewed toward shorter sequences.  We use a log-uniform
    distribution to approximate this.
    """
    import math
    rng = random.Random(seed)
    log_min = math.log(min_len)
    log_max = math.log(max_len)
    seqs = []
    for i in range(count):
        length = int(math.exp(rng.uniform(log_min, log_max)))
        nuc = "".join(rng.choices("ACGT", k=length))
        seqs.append((f"bench_{i}", nuc))
    return seqs


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

WARMUP_ROUNDS = 1
DEFAULT_TIMED_ROUNDS = 3


def _run_benchmark(sequences: list[tuple[str, str]], timed_rounds: int = DEFAULT_TIMED_ROUNDS, nthreads: int = 0) -> dict:
    """Import (or reimport) scriptoscope and benchmark _find_longest_orf."""
    # Force reimport so code changes are picked up
    if "scriptoscope" in sys.modules:
        mod = importlib.reload(sys.modules["scriptoscope"])
    else:
        import scriptoscope as mod

    batch_fn = getattr(mod, "_find_longest_orfs_batch", None)
    use_batch = batch_fn is not None and nthreads != 1

    fn = mod._find_longest_orf
    cache = mod._longest_orf_cache

    # Warmup (also verifies the function doesn't crash)
    for _ in range(WARMUP_ROUNDS):
        cache.clear()
        if use_batch:
            batch_fn(sequences, nthreads=nthreads)
        else:
            for seq_id, nuc in sequences:
                fn(nuc, seq_id)

    # Timed rounds (clear cache each round to measure raw compute)
    times = []
    for _ in range(timed_rounds):
        cache.clear()
        t0 = time.perf_counter()
        if use_batch:
            batch_fn(sequences, nthreads=nthreads)
        else:
            for seq_id, nuc in sequences:
                fn(nuc, seq_id)
        times.append(time.perf_counter() - t0)

    total_bases = sum(len(nuc) for _, nuc in sequences)

    effective_threads = nthreads if nthreads > 0 else (os.cpu_count() or 1)
    if not use_batch:
        effective_threads = 1

    return {
        "median": statistics.median(times),
        "mean": statistics.mean(times),
        "min": min(times),
        "max": max(times),
        "stdev": statistics.stdev(times) if len(times) > 1 else 0.0,
        "rounds": timed_rounds,
        "sequences": len(sequences),
        "total_bases": total_bases,
        "threads": effective_threads,
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:.2f} ms"


def _delta_str(current: float, baseline: float) -> str:
    if baseline == 0:
        return "  (n/a)"
    ratio = current / baseline
    pct = (ratio - 1) * 100
    sign = "+" if pct >= 0 else ""
    if abs(pct) < 0.5:
        return "  (~same)"
    return f"  {sign}{pct:.1f}% ({'slower' if pct > 0 else 'faster'})"


def _fmt_bases(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} Gb"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} Mb"
    if n >= 1_000:
        return f"{n / 1_000:.1f} kb"
    return f"{n} b"


def _print_results(
    result: dict,
    original: dict | None = None,
    previous: dict | None = None,
    run_number: int = 1,
):
    seqs = result['sequences']
    bases = result['total_bases']
    threads = result.get('threads', 1)
    throughput = bases / result['median'] if result['median'] > 0 else 0

    print()
    print(f"{'=' * 68}")
    print(f"  Run #{run_number}  —  {seqs:,} seqs, {_fmt_bases(bases)}, "
          f"{result['rounds']} rounds, {threads} thread{'s' if threads != 1 else ''}")
    print(f"{'=' * 68}")
    print(f"  Median : {_fmt_ms(result['median'])}", end="")
    if original and run_number > 1:
        print(f"  vs original: {_delta_str(result['median'], original['median'])}", end="")
    if previous and previous is not original:
        print(f"  vs prev: {_delta_str(result['median'], previous['median'])}", end="")
    print()
    print(f"  Mean   : {_fmt_ms(result['mean'])}")
    print(f"  Min    : {_fmt_ms(result['min'])}")
    print(f"  Max    : {_fmt_ms(result['max'])}")
    print(f"  Stdev  : {_fmt_ms(result['stdev'])}")
    print(f"  Thru   : {_fmt_bases(int(throughput))}/s  ({throughput / 1_000_000:.1f} Mb/s)")
    print(f"{'=' * 68}")
    print()


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------

def _watch_loop(sequences: list[tuple[str, str]], timed_rounds: int = DEFAULT_TIMED_ROUNDS, nthreads: int = 0):
    """Poll scriptoscope.py for changes; re-run benchmark on modification."""
    target = ROOT / "scriptoscope.py"
    last_mtime = target.stat().st_mtime

    print("Running initial benchmark...")
    original = _run_benchmark(sequences, timed_rounds, nthreads)
    _print_results(original, run_number=1)
    previous = original
    run_number = 1

    print(f"Watching {target.name} for changes... (Ctrl+C to stop)\n")
    try:
        while True:
            time.sleep(0.5)
            mtime = target.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                # Small delay to let the editor finish writing
                time.sleep(0.3)
                print(f"Change detected — re-running benchmark...")
                try:
                    result = _run_benchmark(sequences, timed_rounds, nthreads)
                    run_number += 1
                    _print_results(result, original=original, previous=previous, run_number=run_number)
                    previous = result
                except Exception as e:
                    print(f"\n  ERROR: {e}\n")
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_int_flag(name: str, default: int) -> int:
    for i, arg in enumerate(sys.argv):
        if arg == name and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return default


def main():
    count    = _parse_int_flag("--count", 100_000)
    min_len  = _parse_int_flag("--min-len", 200)
    max_len  = _parse_int_flag("--max-len", 35_000)
    rounds   = _parse_int_flag("--rounds", DEFAULT_TIMED_ROUNDS)
    nthreads = _parse_int_flag("--threads", 0)  # 0 = auto

    print(f"Generating {count:,} sequences ({min_len}–{max_len} nt, log-uniform)...")
    sequences = _generate_sequences(count=count, min_len=min_len, max_len=max_len)
    total = sum(len(nuc) for _, nuc in sequences)
    print(f"  Total: {_fmt_bases(total)}  —  avg {total // count:,} nt/seq\n")

    if "--watch" in sys.argv:
        _watch_loop(sequences, rounds, nthreads)
    else:
        result = _run_benchmark(sequences, rounds, nthreads)
        _print_results(result, run_number=1)


if __name__ == "__main__":
    main()
