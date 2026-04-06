#!/usr/bin/env python3
# Run with: .venv/bin/python devtools/bench_orf_compare.py
"""Compare _find_longest_orf against orfipy on identical workloads.

Usage:
    python devtools/bench_orf_compare.py [OPTIONS]

Options:
    --count N      Number of sequences (default: 100000)
    --max-len N    Max nucleotide length (default: 35000)
    --min-len N    Min nucleotide length (default: 200)
    --rounds N     Timed rounds per engine (default: 3)
    --threads N    Threads for scriptoscope batch (default: 0 = auto)
    --validate     Cross-validate results between engines (slower)
"""
from __future__ import annotations

import math
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Sequence generation (same as bench_orf.py)
# ---------------------------------------------------------------------------

def _generate_sequences(
    seed: int = 42,
    count: int = 100_000,
    min_len: int = 200,
    max_len: int = 35_000,
) -> list[tuple[str, str]]:
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
# Engines
# ---------------------------------------------------------------------------

def _bench_scriptoscope_single(sequences, rounds):
    """ScriptoScope _find_longest_orf, one at a time (C or fallback)."""
    import scriptoscope as mod
    fn = mod._find_longest_orf
    cache = mod._longest_orf_cache

    # Warmup
    cache.clear()
    for sid, nuc in sequences[:1000]:
        fn(nuc, sid)

    times = []
    for _ in range(rounds):
        cache.clear()
        t0 = time.perf_counter()
        for sid, nuc in sequences:
            fn(nuc, sid)
        times.append(time.perf_counter() - t0)
    return times


def _bench_scriptoscope_batch(sequences, rounds, nthreads):
    """ScriptoScope _find_longest_orfs_batch (parallel C)."""
    import scriptoscope as mod
    batch_fn = mod._find_longest_orfs_batch
    cache = mod._longest_orf_cache

    # Warmup
    cache.clear()
    batch_fn(sequences[:1000], nthreads=nthreads)

    times = []
    for _ in range(rounds):
        cache.clear()
        t0 = time.perf_counter()
        batch_fn(sequences, nthreads=nthreads)
        times.append(time.perf_counter() - t0)
    return times


def _bench_orfipy(sequences, rounds):
    """orfipy: find all ORFs, pick longest (fair comparison)."""
    import orfipy_core as oc

    _starts = ["ATG"]
    _stops = ["TAA", "TAG", "TGA"]
    _minlen = 90  # 30 aa * 3

    # Warmup
    for _, nuc in sequences[:1000]:
        oc.orfs(nuc, minlen=_minlen, starts=_starts, stops=_stops, strand="b")

    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _, nuc in sequences:
            orfs = oc.orfs(nuc, minlen=_minlen, starts=_starts, stops=_stops, strand="b")
            # Pick longest to match our "find longest ORF" behavior.
            if orfs:
                max(orfs, key=lambda o: o[1] - o[0])
        times.append(time.perf_counter() - t0)
    return times


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(sequences, sample=2000):
    """Cross-validate: do both engines agree on the longest ORF length?"""
    import scriptoscope as mod
    import orfipy_core as oc

    rng = random.Random(99)
    indices = rng.sample(range(len(sequences)), min(sample, len(sequences)))

    mismatches = 0
    for idx in indices:
        sid, nuc = sequences[idx]
        mod._longest_orf_cache.clear()
        ours = mod._find_longest_orf(nuc, sid)
        our_len = ours.aa_length if ours else 0

        orfs = oc.orfs(nuc, minlen=90, starts=["ATG"], stops=["TAA", "TAG", "TGA"], strand="b")
        if orfs:
            longest = max(orfs, key=lambda o: o[1] - o[0])
            their_len = (longest[1] - longest[0]) // 3
        else:
            their_len = 0

        if our_len != their_len:
            mismatches += 1
            if mismatches <= 5:
                print(f"  [{idx}] ours={our_len}aa  orfipy={their_len}aa  len={len(nuc)}")

    return mismatches, len(indices)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt_ms(s): return f"{s * 1000:.0f} ms"
def _fmt_s(s):  return f"{s:.2f}s"

def _fmt_bases(n):
    if n >= 1_000_000: return f"{n / 1_000_000:.1f} Mb"
    if n >= 1_000: return f"{n / 1_000:.1f} kb"
    return f"{n} b"


def _print_comparison(results: dict, total_bases: int, count: int):
    print()
    print(f"{'=' * 72}")
    print(f"  {count:,} sequences, {_fmt_bases(total_bases)} total")
    print(f"{'=' * 72}")
    print(f"  {'Engine':<35s} {'Median':>10s} {'Throughput':>14s} {'Speedup':>10s}")
    print(f"  {'-'*35} {'-'*10} {'-'*14} {'-'*10}")

    # Sort by median time
    sorted_engines = sorted(results.items(), key=lambda kv: kv[1]["median"])
    fastest = sorted_engines[0][1]["median"]

    for name, r in sorted_engines:
        med = r["median"]
        thru = total_bases / med if med > 0 else 0
        speedup = med / fastest if fastest > 0 else 0
        mark = " <-- fastest" if med == fastest else ""
        if speedup > 1.01:
            speed_str = f"{1/speedup:.2f}x"
        else:
            speed_str = "1.00x"
        print(f"  {name:<35s} {_fmt_ms(med):>10s} {_fmt_bases(int(thru))}/s{' ':>4s} {speed_str:>10s}{mark}")

    print(f"{'=' * 72}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_int_flag(name, default):
    for i, arg in enumerate(sys.argv):
        if arg == name and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return default


def main():
    count    = _parse_int_flag("--count", 100_000)
    min_len  = _parse_int_flag("--min-len", 200)
    max_len  = _parse_int_flag("--max-len", 35_000)
    rounds   = _parse_int_flag("--rounds", 3)
    nthreads = _parse_int_flag("--threads", 0)
    validate = "--validate" in sys.argv

    if nthreads <= 0:
        nthreads = os.cpu_count() or 1

    print(f"Generating {count:,} sequences ({min_len}–{max_len} nt, log-uniform)...")
    sequences = _generate_sequences(count=count, min_len=min_len, max_len=max_len)
    total_bases = sum(len(nuc) for _, nuc in sequences)
    print(f"  Total: {_fmt_bases(total_bases)}  —  avg {total_bases // count:,} nt/seq")
    print()

    if validate:
        print("Cross-validating on 2000 random sequences...")
        mismatches, checked = _validate(sequences)
        if mismatches:
            print(f"  WARNING: {mismatches}/{checked} mismatches (may differ on tie-breaking)\n")
        else:
            print(f"  OK: {checked} sequences agree.\n")

    results = {}

    # orfipy
    print(f"Benchmarking orfipy ({rounds} rounds)...")
    try:
        times = _bench_orfipy(sequences, rounds)
        results["orfipy (Cython)"] = {
            "median": statistics.median(times),
            "times": times,
        }
        print(f"  median: {_fmt_ms(statistics.median(times))}")
    except ImportError:
        print("  SKIPPED (orfipy not installed)")

    # ScriptoScope single-threaded
    print(f"Benchmarking scriptoscope single-thread ({rounds} rounds)...")
    times = _bench_scriptoscope_single(sequences, rounds)
    results["scriptoscope (1 thread)"] = {
        "median": statistics.median(times),
        "times": times,
    }
    print(f"  median: {_fmt_ms(statistics.median(times))}")

    # ScriptoScope batch
    print(f"Benchmarking scriptoscope batch {nthreads} threads ({rounds} rounds)...")
    try:
        times = _bench_scriptoscope_batch(sequences, rounds, nthreads)
        results[f"scriptoscope ({nthreads} threads)"] = {
            "median": statistics.median(times),
            "times": times,
        }
        print(f"  median: {_fmt_ms(statistics.median(times))}")
    except Exception as e:
        print(f"  SKIPPED ({e})")

    _print_comparison(results, total_bases, count)


if __name__ == "__main__":
    main()
