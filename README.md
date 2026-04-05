# ScriptoScope

A terminal-based (TUI) transcriptome browser for exploring, annotating, and analyzing transcript sequences. Built with [Textual](https://textual.textualize.io/) and [Rich](https://rich.readthedocs.io/).

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Tests](https://img.shields.io/badge/tests-105%20passing-brightgreen)

## Correctness is sacred

Bioinformatics tools live or die by whether you can trust their output. A silent parsing error, a typo in a codon table, or an off-by-one in a length calculation produces plausible-looking wrong answers that poison everything downstream. ScriptoScope treats this as a non-negotiable property:

- **Every push runs 105 tests**, 44 of them dedicated to DNA parsing and genetic-code correctness. These are not optional, and they are not mocked.
- **The standard genetic code is cross-validated against Biopython's authoritative table** on every test run — if NCBI ever updated the stop-codon set and Biopython updated with it, we'd find out immediately.
- **The codon-scan regex is exhaustively tested** against all 64 possible DNA codons. It must match exactly `{ATG, TAA, TAG, TGA}` and nothing else. One typo here would break every ORF call in the app.
- **Every stop codon is tested individually** (`TAA`, `TAG`, `TGA`) and every "similar-looking" non-stop (`TAT`, `TGT`, `TGG`, etc.) is tested to make sure it is *not* treated as a stop. This catches a whole class of regex-edit bugs.
- **The fast ORF scanner is cross-validated against the Biopython-based slow path** on 200+ random sequences per test run — both paths must agree on every ORF length, every time.
- **FASTA parsing preserves bytes exactly.** Multi-line FASTA with varied line widths, gzipped FASTA, trailing whitespace, N bases, and edge lengths are all verified to round-trip byte-for-byte.
- **Length arithmetic is triple-checked**: `Transcript.length`, `len(sequence)`, and `sum(nucleotide_counts.values())` must all agree on every sequence we construct.

If any of these tests fail, the push is broken. No exceptions.

## Features

- **FASTA browser** -- load and browse transcriptomes with instant filtering by ID, description, length, or GC content
- **Sequence viewer** -- colorized nucleotide display with CDS and Pfam domain annotations overlaid on the DNA sequence
- **6-frame ORF detection** -- identifies the longest coding sequence (CDS) across all six reading frames
- **Pfam domain scanning** -- scan transcripts against the Pfam HMM database using [pyhmmer](https://pyhmmer.readthedocs.io/) with progress tracking
- **CDS confirmation via NCBI BLAST** -- submit the putative CDS protein to NCBI blastp (SwissProt) to confirm it matches a known protein
- **Local BLAST** -- run BLAST+ searches against local databases or the loaded transcriptome
- **Statistics** -- per-transcript and collection-wide stats (length distribution, GC content, N50, nucleotide composition)
- **Visualization** -- transcript diagram showing CDS location within the transcript, protein track with Pfam domain positions, and domain legend

## Installation

```bash
# Clone the repository
git clone https://github.com/Binomica-Labs/ScriptoScope.git
cd ScriptoScope

# Install dependencies
pip install -r requirements.txt

# Or install as a package
pip install .
```

### Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| textual | >= 0.61.0 | TUI framework |
| biopython | >= 1.83 | Sequence parsing, NCBI BLAST |
| pyhmmer | >= 0.10.0 | HMM profile searching (Pfam) |
| rich | >= 13.7.0 | Terminal rendering |

Optional: [BLAST+](https://blast.ncbi.nlm.nih.gov/doc/blast-help/downloadblastdata.html) command-line tools for local BLAST searches.

## Usage

```bash
# Launch with a FASTA file
python scriptoscope.py /path/to/transcriptome.fasta

# Or launch and open a file from the UI
python scriptoscope.py

# If installed as a package
scriptoscope /path/to/transcriptome.fasta
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Ctrl+O` | Open FASTA file |
| `Ctrl+S` | Save project (transcriptome + analysis cache) |
| `Ctrl+G` | Search GenBank (TSA) |
| `Ctrl+F` | Focus filter input |
| `Ctrl+B` | Switch to BLAST tab |
| `Ctrl+P` | Switch to Pfam/HMM tab |
| `Ctrl+C` | Copy CDS / highlighted region |
| `Ctrl+R` | Copy reverse complement of highlighted DNA |
| `Ctrl+D` | Toggle bookmark on selected transcript |
| `Ctrl+E` | Export bookmarked transcripts as FASTA |
| `?` | Show help |
| `Ctrl+Q` | Quit |

## Tabs

### Sequence

Displays the selected transcript's nucleotide sequence with per-base coloring designed so GC content is visible at a glance: **A = cyan**, **T = lime green** (cool colors for A/T pairs), **C = orange**, **G = red** (warm colors for G/C pairs). GC-rich regions show up as bands of warm color. After running a Pfam scan, the view automatically updates to show:

- **Amino acid translation** aligned beneath the DNA, with each residue centered on its codon
- **Pfam domain bars** spanning the corresponding nucleotide positions

### BLAST

Run local BLAST+ searches. Supports blastn, tblastn, blastx, blastp, and tblastx. Can use the loaded transcriptome as a database or specify a custom database path.

### Pfam / HMM

Scan transcripts for Pfam protein domains:

- **Scan Selected** -- scans all 6-frame ORFs of the selected transcript against the Pfam database
- **Scan Collection (PFAM)** -- scans the entire loaded transcriptome (warns about runtime and shows elapsed time + ETA)
- **Confirm CDS (NCBI)** -- finds the longest ORF and submits the translated protein to NCBI blastp against SwissProt to confirm it is a real protein
- **Download Pfam** -- automatically downloads and indexes the Pfam-A HMM database (~1.5 GB)

The visualization shows:
1. A transcript track with the CDS highlighted (colored by reading frame) and UTR regions
2. A protein track with Pfam domain positions shown as colored blocks
3. Individual domain rows with names and coordinate ranges
4. A legend with family name, accession, E-value, score, and description
5. BLAST confirmation status (CONFIRMED/UNCONFIRMED) with top hit details

### Statistics

Collection-wide and per-transcript statistics including total bases, length distribution histogram, N50, GC content, and nucleotide composition.

## Filtering

The filter bar supports multiple criteria separated by spaces:

| Filter | Example | Matches |
|--------|---------|---------|
| Text | `kinase` | ID or description contains "kinase" |
| Length | `>500` or `<1000` | Transcript length threshold |
| GC content | `gc>45` or `gc<60` | GC percentage threshold |
| Pfam domain | `pfam:PF00069` or `pfam:kinase` | Transcripts with matching Pfam hits (after collection scan) |

## Debugging & bug reports

Every session writes a structured log. If you hit a bug, grab the last few hundred lines of the log file and share them — the log includes everything needed to diagnose the failure remotely.

**Log location**: `/tmp/scriptoscope.log` by default. Override with the `SCRIPTOSCOPE_LOG` environment variable (e.g. `SCRIPTOSCOPE_LOG=~/scriptoscope.log scriptoscope ...`).

**Rotation**: the file is capped at 5 MB with 3 backups (`scriptoscope.log.1`, `.log.2`, `.log.3`) — it can't grow unbounded.

**Finding a specific session**: every session logs a startup banner with a unique 8-character session ID, and every log line is tagged with that ID. To extract just one session:

```bash
# Find recent session IDs
grep "session .* starting" /tmp/scriptoscope.log | tail

# Dump everything from one session
grep "\[a1b2c3d4\]" /tmp/scriptoscope.log
```

**What the banner captures**:

```
ScriptoScope session a1b2c3d4 starting
  version         : 0.6.0
  python          : 3.12.3 (CPython)
  platform        : Linux-...
  cwd             : /home/user/work
  argv            : ['scriptoscope', 'my.fasta']
  log file        : /tmp/scriptoscope.log
  pid             : 12345
  textual         : 8.2.2
  rich            : 14.3.3
  biopython       : 1.87
  pyhmmer         : 0.12.0
```

**Unhandled exceptions**: both main-thread and worker-thread uncaught exceptions are captured with full tracebacks via `sys.excepthook` and `threading.excepthook`. If ScriptoScope crashes silently, the log will still have the stack trace.

## Testing

```bash
pytest tests/ -q
```

The test suite is run on every push and every local commit. It is organized into two files:

| File | Focus | Tests |
|------|-------|-------|
| `tests/test_smoke.py` | End-to-end app behavior: FASTA loading, filters, stats, ORF finding, BLAST/HMMER panels, widget wiring | 61 |
| `tests/test_dna_sanity.py` | **Sacred territory** — codon table correctness, regex exhaustiveness, reverse complement, hand-crafted ORF ground truth, cross-validation with Biopython, FASTA byte-exact parsing, length arithmetic | 44 |

`test_dna_sanity.py` exists specifically to make sure the fundamentals stay correct across every refactor. If a future change to the ORF scanner, codon regex, or FASTA parser introduces a subtle error, these tests will catch it before the push lands. They are deliberately boring and repetitive — that is the point.

## Changelog

### v0.6.0 — Correctness and robustness pass

- **44 new data-integrity tests** covering the standard genetic code, the codon-scan regex, reverse complement, hand-crafted ORFs in all frames and on both strands, cross-validation of the fast ORF path against the Biopython-backed slow path, byte-exact FASTA parsing (including gzip and multi-line), and length arithmetic
- **Gzip FASTA support** via magic-byte detection — `.fasta.gz` files open transparently
- **Duplicate transcript IDs** are renamed with a `__N` suffix instead of silently overwriting
- **`FastaFormatError`** raised for files with content but no `>` header (was previously silent)
- **Atomic project saves** (`save_project`) via temp-file + `fsync` + `os.replace`
- **Project version validation** with `ProjectFormatError` — future or malformed project files are rejected with a clear message instead of silently misparsed
- **Tolerant dataclass deserialization** for forward/backward-compat drift in saved projects
- **In-dialog save feedback**: `Ctrl+S` now shows "Saving…", then "Saved to <file>" in the dialog and auto-closes
- **7x faster collection ORF scan** via a regex-based codon scanner that replaces Biopython's `Seq.translate()` on the hot path; dead `ORFCoord` allocations eliminated
- **Two-phase stats** — length/GC/buckets/N50 show immediately on load, ORF stats fill in asynchronously with a "scanning 6 frames…" placeholder
- **Debounced transcript selection (35 ms)** coalesces rapid arrow-key scrolling into single panel updates
- **Stats panel rebuild avoided** on re-selection; global tables cached across switches
- **Filter runs off the main thread** for transcriptomes larger than 5,000 entries
- **NCBI BLAST RID cleanup** on user cancel — server-side jobs are released instead of orphaned
- **HMM and BLAST database paths** are validated before scans start, with clear errors
- **In-flight scans cancelled on new load**; all per-transcript caches cleared to prevent cross-dataset leaks
- **Biopython partial-codon warnings silenced** at the source (slice to codon boundary before translate)
- Numerous performance micro-fixes: string concatenation replaced with `list.join`, `str.find` loops for 'M' scanning, single-cell bookmark toggle, buffered FASTA export, `Event.wait` polling in NCBI BlastP

### v0.5.0

- Export CSV from BLAST/Pfam/Statistics panels
- Sortable transcript table (ID / Length / GC%)
- Bookmarks (`Ctrl+D`) and bookmarked-FASTA export (`Ctrl+E`)
- Help modal (`?`)
- ORF statistics in the stats panel
- Go-to position input in the sequence viewer
- Smoke test suite (61 tests)

### v0.4.0

- 6-frame ORF detection with longest-CDS identification
- Pfam domain scanning via pyhmmer with HMM database caching and progress throttling
- NCBI blastp CDS confirmation against SwissProt
- Annotated sequence viewer with amino acid translation and Pfam domain overlay on DNA
- Transcript + protein visualization diagram with domain legend
- Collection scan with elapsed time and ETA display
- Terminal-safe quit during long-running scans
- Debug logging to `/tmp/scriptoscope.log`

### v0.3.0

- Consolidated into single-file script
- Fixed transcript selection (auto-select first row on load)
- Moved stats computation off the main thread

### v0.2.0

- File-browser dialog
- Scrollable widgets, thread-safe UI updates
- Run-length encoded sequence coloring
- Single-pass statistics

### v0.1.0

- Initial release: FASTA loading, sequence viewer, BLAST, HMMER, statistics

## License

MIT

## Contributing

Issues and pull requests welcome at [github.com/Binomica-Labs/ScriptoScope](https://github.com/Binomica-Labs/ScriptoScope).
