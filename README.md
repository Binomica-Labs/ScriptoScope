# ScriptoScope

A terminal-based (TUI) transcriptome browser for exploring, annotating, and analyzing transcript sequences. Built with [Textual](https://textual.textualize.io/) and [Rich](https://rich.readthedocs.io/).

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

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
| `Ctrl+F` | Focus filter input |
| `Ctrl+B` | Switch to BLAST tab |
| `Ctrl+P` | Switch to Pfam/HMM tab |
| `Ctrl+D` | Build BLAST database from loaded transcriptome |
| `Q` | Quit |

## Tabs

### Sequence

Displays the selected transcript's nucleotide sequence with per-base coloring (A=green, T=red, G=yellow, C=cyan). After running a Pfam scan, the view automatically updates to show:

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

## Changelog

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
