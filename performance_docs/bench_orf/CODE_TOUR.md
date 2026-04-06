# Code Tour: `_find_longest_orf` and Supporting Functions

## Purpose

`_find_longest_orf` finds the single longest methionine-initiated open reading
frame (ORF) across all six reading frames (3 forward + 3 reverse-complement) of
a nucleotide sequence. It returns an `ORFCoord` describing the winning ORF, or
`None` if nothing reaches the 30-amino-acid minimum.

This is the **fast path** (~10x faster than the reference implementation
`_six_frame_orf_coords`, which delegates per-codon translation to Biopython).
The two implementations are cross-validated in `tests/test_dna_sanity.py`.

---

## Function Map

| Symbol | Location | Role |
|---|---|---|
| `_find_longest_orf` | `scriptoscope.py:1152-1243` | Main entry point |
| `_CODON_SCAN_RE` | `scriptoscope.py:1253` | Compiled regex that locates all ATG/stop positions |
| `_RC_TABLE` | `scriptoscope.py:1246` | `str.maketrans` table for reverse-complement |
| `_seq_fingerprint` | `scriptoscope.py:1107-1112` | Cheap hash for cache keys |
| `_translate_orf_dna` | `scriptoscope.py:1137-1149` | Codon-table translation (no Biopython) |
| `_ORF_CODON_TABLE` | `scriptoscope.py:1117-1134` | Standard genetic code dict (64 entries) |
| `_longest_orf_cache` | `scriptoscope.py:1103` | `OrderedDict` LRU cache (max 4096) |
| `ORFCoord` | `scriptoscope.py:999-1009` | Dataclass for the result |
| `_six_frame_orf_coords` | `scriptoscope.py:1046-1096` | Slow reference implementation (Biopython) |

---

## Algorithm Walkthrough

### Phase 1 -- Cache check (lines 1161-1164)

```
key = (seq_id, len(nucleotide), _seq_fingerprint(nucleotide))
```

The cache key combines the sequence ID, its length, and a lightweight hash.
`_seq_fingerprint` avoids hashing the full string for long sequences -- it
samples the first 64, middle 64, and last 64 characters and hashes that tuple.
On a cache hit, the entry is moved to the end (LRU) and returned immediately.

### Phase 2 -- Sequence preparation (lines 1166-1168)

```
upper = nucleotide.upper()
rc    = upper.translate(_RC_TABLE)[::-1]
```

Two strings are produced: the uppercased forward strand and its
reverse-complement. `_RC_TABLE` is a `str.maketrans("ACGTN", "TGCAN")` table,
so `.translate()` swaps bases in O(n), and `[::-1]` reverses the result.

### Phase 3 -- Regex scan + frame partitioning (lines 1176-1181)

**This is the dominant loop.**

```python
for strand_label, strand_seq in (("+", upper), ("-", rc)):      # 2 iterations
    frames: list[list[tuple[int, str]]] = [[], [], []]
    for m in _CODON_SCAN_RE.finditer(strand_seq):                # O(n) regex scan
        p = m.start()
        frames[p % 3].append((p, m.group(1)))
```

The regex `(?=(ATG|TAA|TAG|TGA))` uses a **lookahead** so that overlapping
codons in different reading frames are all captured (e.g. `TAATG` yields TAA at
position 0 and ATG at position 2). Each match is bucketed into one of three
frame lists based on `position % 3`.

**Cost:** The regex engine walks the full strand once per strand (~2n total
characters). This is the most expensive step -- it produces O(m) match objects,
where m is the number of start/stop codons in the sequence.

### Phase 4 -- Best-ORF scan (lines 1182-1197)

```python
    for frame_idx, frame_hits in enumerate(frames):          # 3 frames
        first_atg = -1
        for p, codon in frame_hits:                          # O(k) per frame
            if codon == "ATG":
                if first_atg == -1:
                    first_atg = p
            else:                       # stop codon
                if first_atg != -1:
                    length = (p - first_atg) // 3
                    if length > best_length:
                        best_length   = length
                        best_strand   = strand_label
                        best_frame    = frame_idx
                        best_strand_m = first_atg
                        best_strand_stop = p
                    first_atg = -1
```

For each frame, this walks the pre-sorted list of codon hits and tracks the
first ATG seen. When a stop codon is reached, the span from `first_atg` to the
stop defines a candidate ORF. The longest candidate across all 6 frames wins.

Key detail: `first_atg` is only set once per ATG-stop window (subsequent ATGs
before a stop are ignored). This means the function finds the longest ORF that
starts at the **earliest** ATG before each stop, which is the standard
biological convention.

**Cost:** O(m) total across all 6 frames, where m is the number of regex
matches. All comparisons are integer arithmetic -- no allocations in this loop.

### Phase 5 -- Threshold gate (lines 1199-1203)

If the best ORF has fewer than 30 amino acids, the result is cached as `None`
and the function returns early.

### Phase 6 -- Translation (lines 1205-1208)

```python
strand_seq = upper if best_strand == "+" else rc
orf_dna    = strand_seq[best_strand_m:best_strand_stop]
aa_sequence = _translate_orf_dna(orf_dna)
```

`_translate_orf_dna` walks the DNA slice in 3-base steps, looking up each codon
in `_ORF_CODON_TABLE` (a plain dict with 64 entries). Unknown codons (e.g.
containing N) produce `'X'`. It stops at the first `'*'` (stop codon).

**Cost:** O(L/3) where L is the ORF's nucleotide length. One dict lookup per
codon, then a `"".join()` at the end.

### Phase 7 -- Stop-codon counting + coordinate mapping (lines 1210-1227)

A small probe loop counts consecutive in-frame stop codons after the
terminating stop (e.g. TAA-TAA). This affects the nucleotide end coordinate.
Strand-local coordinates are then flipped to original-sequence coordinates for
reverse-strand ORFs.

**Cost:** O(1) in practice (rarely more than 1-2 consecutive stops).

### Phase 8 -- Result construction + caching (lines 1229-1243)

An `ORFCoord` dataclass is built, cached, and returned. The cache uses FIFO
eviction when it exceeds 4096 entries.

---

## Dominating Loops Summary

Ordered by expected cost (highest first):

| # | Loop | Location | Iterations | Notes |
|---|------|----------|-----------|-------|
| 1 | `_CODON_SCAN_RE.finditer(strand_seq)` | line 1179 | O(n) per strand | **Dominant.** Regex engine scans every position. Runs twice (forward + RC). |
| 2 | `upper.translate(_RC_TABLE)[::-1]` | line 1167 | O(n) | String ops; fast in CPython C layer but still full-length. |
| 3 | `nucleotide.upper()` | line 1166 | O(n) | Full copy + case conversion. |
| 4 | Frame-hit scan | lines 1184-1197 | O(m) total | m = number of start/stop matches, typically m << n. |
| 5 | `_translate_orf_dna` | line 1208 | O(L/3) | L = winning ORF length. Only runs once. |
| 6 | Stop-codon probe | lines 1213-1216 | ~1-3 | Negligible. |

The regex scan (loop #1) dominates wall-clock time. The lookahead pattern
`(?=(ATG|TAA|TAG|TGA))` forces the engine to attempt a match at every position
in the string, making this effectively O(n) with a constant factor determined by
the regex engine's alternation strategy. Any performance optimization effort
should focus here first.

---

## Reference Implementation: `_six_frame_orf_coords`

Located at `scriptoscope.py:1046-1096`, this is the slower ground-truth version
that uses Biopython's `Seq.translate()`. It returns **all** ORFs (not just the
longest) and is used only in tests for cross-validation.

The key cost difference: Biopython translates the entire reading frame into a
protein string character-by-character, then splits on `'*'` to find ORF
boundaries. This involves Python-level iteration over every codon plus
Biopython's translation machinery, versus the fast path's single compiled regex
scan that only touches start/stop positions.

---

## Data Flow Diagram

```
nucleotide string
    |
    v
.upper()  -->  upper (forward strand)
    |
    v
.translate(_RC_TABLE)[::-1]  -->  rc (reverse complement)
    |
    v
For each strand in (upper, rc):
    |
    v
_CODON_SCAN_RE.finditer()  -->  [(pos, codon), ...]  <-- DOMINANT COST
    |
    v
Bucket by pos % 3  -->  frames[0], frames[1], frames[2]
    |
    v
Linear scan per frame: track first_atg, measure ATG-to-stop spans
    |
    v
Best ORF across all 6 frames
    |
    v
_translate_orf_dna(orf_dna)  -->  amino acid string
    |
    v
ORFCoord(...)  -->  cached + returned
```
