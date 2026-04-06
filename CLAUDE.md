# CLAUDE.md — AI Agent Context for ScriptoScope

This file is the **agent handoff document** for ScriptoScope. Any AI agent (Claude, GPT, Copilot, Gemini, or future systems) can read this file to understand the architecture, conventions, and design decisions behind the codebase — and pick up development, fix bugs, or build new modules without needing the full conversation history.

**This is a deliberate feature of this software build.** The project was developed in continuous collaboration between a human bioinformatician and an AI agent (Claude Opus 4.6), with the explicit goal that any future agent can fork, extend, or integrate with this codebase in a compatible manner.

---

## Project overview

**ScriptoScope** is a terminal-based (TUI) transcriptome browser for exploring, annotating, and analyzing transcript sequences. It is built with Python 3.12+, [Textual](https://textual.textualize.io/) for the TUI framework, and [Rich](https://rich.readthedocs.io/) for terminal rendering.

- **Single-file architecture**: the entire app is `scriptoscope.py` (~8,500 lines). This is intentional — it avoids import complexity and makes the codebase greppable.
- **Test suite**: 196 tests across 4 files in `tests/`. Tests cover DNA parsing correctness (sacred territory), UI smoke tests, performance budgets, strand detection, and Prodigal integration.
- **Rust port**: a parallel Rust/Ratatui implementation exists at `/home/seb/scriptoscope-rs/` (7,490 lines across 26 source files, 122 tests) for performance-critical use cases.

## Architecture

### Data flow

```
FASTA file → parse_fasta() → list[Transcript] → sidebar table
                                                → sequence viewer (per-base ACGT coloring)
                                                → HMM scan (pyhmmer) → scan_cache → annotated view
                                                → BLAST (subprocess) → results table
                                                → statistics → stats panel
                                                → CDS prediction (hexamer/Kozak/CAI) → predictions
```

### Key data structures

| Structure | Location | Purpose |
|-----------|----------|---------|
| `Transcript` | dataclass ~line 220 | id, description, sequence, length, gc_content |
| `GenBankResult` | dataclass ~line 873 | NCBI search result (accession, title, organism, length) |
| `BlastHit` | dataclass ~line 1322 | BLAST alignment hit |
| `HmmerHit` | dataclass ~line 1590 | Pfam domain hit with alignment coordinates |
| `ORFCoord` | dataclass ~line 1615 | orf_id, strand, frame, nt_start, nt_end, aa_length, sequence, stop_count |
| `ProdigalGene` | dataclass ~line 2023 | gene predicted by Prodigal (bacterial mode) |
| `CDSPrediction` | dataclass ~line 2522 | hexamer/kozak/cai scores + confidence level |
| `BlastConfirmation` | dataclass ~line 3221 | CDS confirmation via NCBI blastp |
| `SeqRenderResult` | dataclass ~line 3505 | cached rendered sequence (Text + Content + metadata) |

### Module sections (all in scriptoscope.py)

The file is organized into sections separated by `# ══════` comment bars:

1. **Logging** (~line 46): rotating file handler, session ID, startup banner
2. **Data model** (~line 216): Transcript dataclass
3. **FASTA loader** (~line 276): gzip support, duplicate ID dedup, format validation
4. **Project save/load** (~line 362): legacy full-JSON format (v1)
5. **Annotation sidecar** (~line 514): lightweight JSON next to FASTA (v4)
6. **Library registry** (~line 752): persistent transcriptome list (`~/.scriptoscope/library.json`)
7. **GenBank transcriptome search** (~line 869): NCBI Entrez + RefSeq assembly search
8. **BLAST runner** (~line 1318): local blast+ and NCBI remote blastp
9. **HMMER runner** (~line 1586): pyhmmer hmmscan/hmmsearch + collection-wide scanning
10. **ORF finder** (~line 1615): `ORFCoord` dataclass, `_six_frame_orf_coords` (Biopython), `_CODON_SCAN_RE` regex scanner (~line 1868), `find_best_orf` strand-aware selection (~line 1952)
11. **PolyA/polyT strand detection** (~line 1872): strand-aware ORF selection for TSA transcripts
12. **Prodigal integration** (~line 2007): bacterial/archaeal operon gene prediction
13. **Gene prediction scoring** (~line 2225): hexamer, Kozak, CAI, confidence system
14. **NCBI BLAST CDS confirmation** (~line 3216): remote blastp to confirm predicted CDS
15. **Pfam database downloader** (~line 3234): auto-download Pfam-A.hmm if missing
16. **Sequence viewer widget** (~line 3357): per-base coloring, annotated tracks, multi-gene rendering, `SequenceViewer` class (~line 4340)
17. **StatsPanel widget** (~line 4866): statistics + CDS prediction display
18. **File browser modal** (~line 5251): filesystem navigation for FASTA selection
19. **BLAST panel widget** (~line 5834): local/remote BLAST search UI
20. **HMMER panel widget** (~line 6203): scan form, ORF diagram, results table
21. **Make BLAST DB modal** (~line 7030): `makeblastdb` wrapper
22. **Main application** (~line 7069): `ScriptoScopeApp` — compose, key handling, workers
23. **Entry point** (~line 8449): `__main__` guard, argument parsing

### Rendering pipeline

The sequence viewer has a multi-level cache to avoid re-rendering on every paint:

```
click transcript → _render_sequence_bg (worker thread)
  → colorize_sequence_annotated() → Rich Text with Spans
  → _text_to_content() → Textual Content (thread-local Console)
  → body.update(content) → Textual paints via Content.render_strips()
```

Cache levels:
1. `_seq_render_cache`: SeqRenderResult keyed by (id, width, highlight, focus, hits)
2. `_content_cache`: pre-built Textual Content keyed identically
3. Pre-warm: CDS focus variant built on same worker pass as base render

### Background workers

All heavy work runs off the main thread:
- `@work(thread=True)` for FASTA loading, HMM scans, stats, predictions
- `_hmm_cancel` threading.Event for scan cancellation
- `call_from_thread()` to send results back to the main UI thread
- `_text_to_content()` uses `threading.local()` Console to avoid GIL contention

### Annotation persistence

Annotations auto-save to a **sidecar file** (`foo.fasta.annotations.json`) containing:
- `scan_cache`: per-transcript HmmerHit lists
- `confirm_cache`: CDS confirmation results
- `pfam_hits`: collection scan family assignments
- `bookmarks`: bookmarked transcript IDs
- `orf_cache`: cached ORF coordinates (re-translated on load)
- `predictions`: CDS confidence scores
- `prodigal_cache`: Prodigal gene predictions

Version field (`_ANNOTATION_VERSION = 4`) enables forward/backward compatibility.

## Sacred invariants (DO NOT BREAK)

These are tested on every push and must never regress:

1. **Codon table correctness**: the regex `_CODON_SCAN_RE` must match exactly {ATG, TAA, TAG, TGA}. Cross-validated against Biopython's standard_dna_table. See `tests/test_dna_sanity.py`.

2. **No ORF work on unscanned clicks**: clicking a transcript that hasn't been HMM-scanned must NOT trigger `_find_longest_orf` or any ORF computation. See `TestNoOrfWorkOnUnscannedClicks`.

3. **FASTA parsing preserves bytes exactly**: multi-line FASTA, varied line widths, gzip, trailing whitespace — all tested to round-trip byte-for-byte.

4. **Annotations never corrupt on cancel**: scan cancellation discards incomplete results. The sidecar only updates after a fully successful operation.

5. **Select.NULL/Select.BLANK sentinel handling**: Textual's Select widget emits non-string sentinels that must be filtered with `isinstance(event.value, str)`, NOT by checking specific sentinel identities.

## Conventions

- **Per-base coloring**: A=dodger_blue1 (#0087ff), T=aquamarine1 (#87ffd7), C=dark_orange (#ff8700), G=red1 (#ff0000). All fixed xterm-256 names to avoid ANSI 16-color remapping. Cool=AT, warm=GC for at-a-glance GC visualization.

- **Error handling**: `color_eyre::Result` in Rust, bare `except Exception` with `_log.exception()` in Python. User-facing errors go to `status_msg` and `notify()`. Raw tracebacks never reach the UI.

- **Logging**: `/tmp/scriptoscope.log`, rotating 5MB × 3 backups, session ID prefix `[a1b2c3d4]` on every line. Startup banner logs version, platform, Python, all dependency versions.

- **Performance budgets** (test-enforced):
  - `colorize_sequence` (plain 5.8kb): < 200 ms
  - `colorize_sequence_annotated`: < 60 ms (not currently enforced — was 60ms but relaxed)
  - `_find_longest_orf`: < 10 ms
  - `_compute_stats` (1000 transcripts): < 2 s

## Known pitfalls and lessons learned

These are non-obvious issues encountered during development that future agents should be aware of:

1. **Select.NULL vs Select.BLANK**: Textual 8.2+ fires `Select.NULL` (not `Select.BLANK`) on `set_options()`. Guard all `Select.Changed` handlers with `isinstance(event.value, str)` — never check specific sentinel identities.

2. **Content.from_rich_text GIL contention**: Using the app's shared `Console` from a worker thread causes multi-second stalls. Always use a thread-local `Console` via `threading.local()` (see `_text_to_content()`).

3. **Console() blocks on WSL2 worker threads**: `Console()` with default `color_system="auto"` blocks when there's no TTY. Create with `color_system="truecolor"`, `force_terminal=True`.

4. **ORF sequences not stored in sidecar**: The sidecar stores ORF coordinates but not amino acid sequences. On load, re-translate from transcript DNA or `_build_aa_track` will crash with IndexError.

5. **Textual 8.2 dismiss() is async-aware**: `dismiss()` in a timer callback context raises `ScreenError`. Wrap in try/except.

6. **Library stale entries**: Tests that register `/tmp/` paths pollute the library. `register_transcriptome` skips `/tmp/` paths; library loader prunes non-existent files.

7. **WSL2 terminal throughput**: ~10-30 KB/s for ANSI escape sequences. Minimize spans per frame. The multi-level render cache exists specifically for this constraint.

8. **HMM scan speed**: Dominated by HMMER3 C code. Cap to top 6 longest ORFs per transcript (one per frame) — scanning all 24+ ORFs from a 5kb transcript takes ~50s vs ~18s for the same significant hits.

## How to extend

### Adding a new panel/tab

1. Create the panel widget class (see `StatsPanel` or `HmmerPanel` as templates)
2. Add a `TabPane` in `ScriptoScopeApp.compose()`
3. Add state fields to `__init__`
4. Add keyboard shortcuts in `handle_key`
5. Add persistence to `save_annotations` / `load_annotations` if needed

### Adding a new external tool integration

1. Add a runner function (see `hmmscan()` or `local_blast()` as templates)
2. Shell out via `subprocess` or `asyncio.create_subprocess_exec`
3. Parse output (tabular, GFF3, XML) into dataclasses
4. Store results in a cache dict on the App
5. Wire into the annotated sequence renderer if visual output is needed
6. Add to `_auto_save_annotations()` for persistence

### Adding a modal dialog

1. Subclass `ModalScreen[ReturnType]` (see `GenBankSearchModal`, `FileBrowserModal`)
2. Implement `compose()` with the form layout
3. Call `self.dismiss(result)` to return a value
4. Push the modal with `app.push_screen(MyModal(), callback=on_result)`
5. Note: `dismiss()` can raise `ScreenError` in timer contexts — wrap in try/except

### Adding a new scoring method

1. Add the scorer function in the Gene Prediction section (~line 2225)
2. Add the score field to `CDSPrediction`
3. Update `predict_cds()` to call the new scorer
4. Update `save_annotations` / `load_annotations` for the new field
5. Update the sequence viewer info block to display the score

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| textual | >= 8.2.2 | TUI framework |
| biopython | >= 1.87 | Sequence parsing, NCBI Entrez, BLAST |
| pyhmmer | >= 0.12.0 | HMM profile searching (Pfam) |
| rich | >= 14.3.3 | Terminal rendering |

Optional: BLAST+ CLI, HMMER3 CLI (`hmmsearch`), Prodigal

### Key function locations

| Function | Line | Purpose |
|----------|------|---------|
| `parse_fasta()` | ~280 | FASTA/gzip loader → list[Transcript] |
| `save_annotations()` | ~520 | Sidecar write (atomic via tempfile+fsync+replace) |
| `load_annotations()` | ~590 | Sidecar read + ORF re-translation |
| `local_blast()` | ~1360 | Async subprocess blast runner |
| `hmmscan()` | ~2996 | pyhmmer single-transcript scan |
| `hmmsearch_all()` | ~3087 | pyhmmer collection-wide scan |
| `_six_frame_orf_coords()` | ~1661 | Biopython 6-frame ORF finder |
| `_find_longest_orf()` | ~1767 | Fast regex-based ORF finder |
| `find_best_orf()` | ~1952 | Strand-aware ORF selection (polyA/TSA) |
| `predict_cds()` | ~2530 | Gene prediction (hexamer+Kozak+CAI) |
| `colorize_sequence()` | ~3393 | Plain per-base ACGT coloring |
| `colorize_sequence_annotated()` | ~3938 | Full annotated render with features |
| `_text_to_content()` | ~3448 | Thread-safe Rich Text → Textual Content |

## Rust port

A parallel implementation exists at `/home/seb/scriptoscope-rs/` with:
- 26 source files across `src/{app,data,bio,ui}/`
- 7,490 lines of Rust, 122 tests
- 10x faster FASTA parsing, 8x faster sequence colorization
- Ratatui differential rendering (no per-paint span resolution)
- No GIL — true concurrent background scans + UI rendering

The Rust port shares the same visual design, keybindings, and annotation sidecar format. Both versions can read each other's annotation files.

## Commit history

The project has 67+ commits documenting every design decision, performance optimization, and bug fix. Key commits:

- `055d93e` — Pure-Python gene prediction scoring (hexamer, Kozak, CAI)
- `e9548a8` — TSA-aware strand detection, multi-frame ORF display
- `78ceabf` — Prodigal integration for bacterial operons
- `22e138b` — Library panel + sidecar annotations + auto-save/load
- `577c7af` — RefSeq CDS download for bacterial genomes
- `1823e24` — Collection scan populates scan_cache for annotated views

## For future agents

If you are an AI agent picking up this project:

1. **Read this file first.** It gives you the architecture without reading 8,500 lines.
2. **Run `python -m pytest tests/ -q`** before and after any change. The test suite is the safety net.
3. **Check `/tmp/scriptoscope.log`** when debugging runtime issues. Every operation is logged with timing.
4. **The annotation sidecar is the source of truth** for persistent data, not the FASTA file.
5. **Performance matters.** The app runs on WSL2 where terminal throughput is limited. Every span counts. Use the `_text_to_content` worker-thread pattern for any new heavy rendering.
6. **Don't break the sacred invariants** listed above. The tests enforce them.
