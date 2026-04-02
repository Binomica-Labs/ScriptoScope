"""
ScriptoScope — TUI Transcriptome Browser
Version 0.3.0

Changelog:
  0.1.0 — initial release: FASTA loading, sequence viewer, BLAST, HMMER, statistics
  0.2.0 — file-browser dialog, scrollable widgets, thread-safe UI updates,
           run-length encoded sequence coloring, single-pass stats
  0.3.0 — consolidated into single-file script, fixed transcript selection
           (RowHighlighted + RowSelected, auto-select first row on load),
           removed VerticalScroll wrapper blocking click events,
           moved _compute_stats off the main thread
  0.4.0 — replaced @on CSS-selector handlers with on_data_table_row_highlighted/
           on_data_table_row_selected method-name convention (reliable in 8.1.1),
           fixed _colorize thread passing body widget to avoid DOM query in thread,
           removed batch_update wrapper that suppressed RowHighlighted during load,
           added error surfacing + debug log at /tmp/scriptoscope.log
"""
from __future__ import annotations

__version__ = "0.4.0"

# ── stdlib ────────────────────────────────────────────────────────────────────
import argparse
import asyncio
import gzip
import logging
import os
import signal
import shutil
import subprocess
import tempfile
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Generator, Optional

# Debug log — tail /tmp/scriptoscope.log to see runtime errors
logging.basicConfig(
    filename="/tmp/scriptoscope.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
_log = logging.getLogger("scriptoscope")

# ── third-party ───────────────────────────────────────────────────────────────
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    ProgressBar,
    RadioButton,
    RadioSet,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)


# ══════════════════════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Transcript:
    id: str
    description: str
    sequence: str
    length: int = field(init=False, repr=False)
    _counts: dict[str, int] | None = field(init=False, repr=False, compare=False, default=None)
    _gc: float | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        self.length = len(self.sequence)

    def _ensure_counts(self) -> None:
        if self._counts is None:
            raw = Counter(self.sequence.upper())
            self._counts = {b: raw.get(b, 0) for b in "ACGTN"}
            gc = self._counts["G"] + self._counts["C"]
            self._gc = (gc / self.length * 100) if self.length > 0 else 0.0

    @property
    def gc_content(self) -> float:
        self._ensure_counts()
        return self._gc  # type: ignore[return-value]

    @property
    def short_id(self) -> str:
        return self.id[:40] + ("…" if len(self.id) > 40 else "")

    def nucleotide_counts(self) -> dict[str, int]:
        self._ensure_counts()
        return dict(self._counts)  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════════════
# FASTA loader
# ══════════════════════════════════════════════════════════════════════════════

def _parse_fasta(path: str | Path) -> Generator[Transcript, None, None]:
    path = Path(path)
    seq_id = None
    description = ""
    seq_parts: list[str] = []

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith(">"):
                if seq_id is not None:
                    yield Transcript(id=seq_id, description=description,
                                     sequence="".join(seq_parts))
                header = line[1:]
                parts = header.split(None, 1)
                seq_id = parts[0]
                description = parts[1] if len(parts) > 1 else ""
                seq_parts = []
            elif seq_id is not None:
                seq_parts.append(line.rstrip())

    if seq_id is not None:
        yield Transcript(id=seq_id, description=description,
                         sequence="".join(seq_parts))


def load_all(path: str | Path) -> list[Transcript]:
    return list(_parse_fasta(path))


# ══════════════════════════════════════════════════════════════════════════════
# BLAST runner
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BlastHit:
    query_id: str
    subject_id: str
    pct_identity: float
    alignment_length: int
    mismatches: int
    gap_opens: int
    query_start: int
    query_end: int
    subject_start: int
    subject_end: int
    evalue: float
    bit_score: float
    subject_description: str = ""


def _parse_blast_tabular(output: str) -> list[BlastHit]:
    hits: list[BlastHit] = []
    for line in output.strip().splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        try:
            hits.append(BlastHit(
                query_id=cols[0], subject_id=cols[1],
                pct_identity=float(cols[2]), alignment_length=int(cols[3]),
                mismatches=int(cols[4]), gap_opens=int(cols[5]),
                query_start=int(cols[6]), query_end=int(cols[7]),
                subject_start=int(cols[8]), subject_end=int(cols[9]),
                evalue=float(cols[10]), bit_score=float(cols[11]),
            ))
        except (ValueError, IndexError):
            continue
    return hits


async def local_blast(
    query_seq: str,
    query_id: str,
    db_path: str,
    program: str = "blastn",
    evalue: float = 1e-5,
    max_hits: int = 50,
    num_threads: int = 4,
) -> tuple[list[BlastHit], str]:
    safe_id = query_id.split()[0] if query_id else "query"
    tmp_fd, query_file = tempfile.mkstemp(suffix=".fasta")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(f">{safe_id}\n{query_seq}\n")
        cmd = [
            program,
            "-query", query_file,
            "-db", db_path,
            "-evalue", str(evalue),
            "-max_target_seqs", str(max_hits),
            "-outfmt", "6",
            "-num_threads", str(num_threads),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        raw = stdout.decode()
        if proc.returncode != 0:
            raise RuntimeError(f"BLAST failed (exit {proc.returncode}):\n{stderr.decode()}")
        return _parse_blast_tabular(raw), raw
    finally:
        Path(query_file).unlink(missing_ok=True)


async def make_blast_db(fasta_path: str, db_type: str = "nucl") -> str:
    db_prefix = fasta_path + ".blastdb"
    cmd = ["makeblastdb", "-in", fasta_path, "-dbtype", db_type,
           "-out", db_prefix, "-parse_seqids"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"makeblastdb failed:\n{stderr.decode()}")
    return db_prefix


@lru_cache(maxsize=1)
def blast_available() -> bool:
    try:
        subprocess.run(["blastn", "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ══════════════════════════════════════════════════════════════════════════════
# HMMER runner
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class HmmerHit:
    target_name: str
    accession: str
    query_name: str
    evalue: float
    score: float
    bias: float
    description: str
    dom_evalue: float = 0.0
    dom_score: float = 0.0
    hmm_from: int = 0
    hmm_to: int = 0
    ali_from: int = 0
    ali_to: int = 0


def hmmer_available() -> bool:
    try:
        import pyhmmer  # noqa: F401
        return True
    except ImportError:
        return False


@dataclass
class ORFCoord:
    """An ORF with its nucleotide-space coordinates."""
    orf_id: str
    strand: str          # "+" or "-"
    frame: int           # 1, 2, or 3
    nt_start: int        # 0-based nucleotide start on the original sequence
    nt_end: int          # 0-based nucleotide end (exclusive) on the original sequence
    aa_length: int
    sequence: str        # amino-acid sequence


def _six_frame_proteins(nucleotide: str, seq_id: str) -> list[tuple[str, str]]:
    from Bio.Seq import Seq
    seq = Seq(nucleotide.upper())
    results = []
    for strand, nuc in (("+", seq), ("-", seq.reverse_complement())):
        for frame in range(3):
            trans = str(nuc[frame:].translate(to_stop=False))
            for i, orf in enumerate(trans.split("*")):
                if len(orf) >= 30:
                    results.append((f"{seq_id}_{strand}f{frame+1}_orf{i}", orf))
    return results


def _six_frame_orf_coords(nucleotide: str, seq_id: str, min_aa: int = 30) -> list[ORFCoord]:
    """Return ORFs with their nucleotide-space coordinates on the original sequence."""
    from Bio.Seq import Seq
    seq = Seq(nucleotide.upper())
    seq_len = len(seq)
    coords: list[ORFCoord] = []
    for strand, nuc in (("+", seq), ("-", seq.reverse_complement())):
        for frame_idx in range(3):
            trans = str(nuc[frame_idx:].translate(to_stop=False))
            aa_offset = 0
            for i, orf in enumerate(trans.split("*")):
                if len(orf) >= min_aa:
                    orf_id = f"{seq_id}_{strand}f{frame_idx+1}_orf{i}"
                    strand_nt_start = frame_idx + aa_offset * 3
                    strand_nt_end = strand_nt_start + len(orf) * 3
                    if strand == "+":
                        nt_start = strand_nt_start
                        nt_end = strand_nt_end
                    else:
                        nt_start = seq_len - strand_nt_end
                        nt_end = seq_len - strand_nt_start
                    coords.append(ORFCoord(
                        orf_id=orf_id, strand=strand, frame=frame_idx + 1,
                        nt_start=nt_start, nt_end=nt_end,
                        aa_length=len(orf), sequence=orf,
                    ))
                aa_offset += len(orf) + 1
    return coords


def _find_longest_orf(nucleotide: str, seq_id: str) -> ORFCoord | None:
    """Find the single longest ORF across all 6 frames (the putative CDS)."""
    orfs = _six_frame_orf_coords(nucleotide, seq_id, min_aa=30)
    if not orfs:
        return None
    return max(orfs, key=lambda o: o.aa_length)


# ── Colors for ORF / domain visualization ────────────────────────────────────

_FRAME_COLORS = {
    ("+", 1): "bright_red",
    ("+", 2): "bright_green",
    ("+", 3): "bright_blue",
    ("-", 1): "red",
    ("-", 2): "green",
    ("-", 3): "blue",
}

_DOMAIN_PALETTE = [
    "bright_magenta", "bright_cyan", "bright_yellow",
    "magenta", "cyan", "yellow",
    "bright_white", "bright_red", "bright_green",
]


def _render_scale(label_w: int, track_w: int, length: int, unit: str = "") -> Text:
    """Render a scale bar + tick marks line."""
    result = Text()
    mid_pos = track_w // 2
    left_label = "0"
    mid_label = f"{length // 2:,}"
    right_label = f"{length:,}{unit}"
    scale_line = [" "] * (track_w + 1)
    for ci, ch in enumerate(left_label):
        if ci < len(scale_line):
            scale_line[ci] = ch
    mid_start = mid_pos - len(mid_label) // 2
    for ci, ch in enumerate(mid_label):
        pos = mid_start + ci
        if 0 <= pos < len(scale_line):
            scale_line[pos] = ch
    right_start = len(scale_line) - len(right_label)
    for ci, ch in enumerate(right_label):
        pos = right_start + ci
        if 0 <= pos < len(scale_line):
            scale_line[pos] = ch
    result.append(" " * label_w)
    result.append("".join(scale_line), style="dim italic")
    result.append("\n")
    # Ticks
    result.append(" " * label_w)
    ticks = [" "] * (track_w + 1)
    ticks[0] = "│"
    ticks[mid_pos] = "│"
    ticks[min(track_w, len(ticks) - 1)] = "│"
    result.append("".join(ticks), style="dim")
    result.append("\n")
    return result


def render_orf_diagram(
    transcript: "Transcript",
    hits: list[HmmerHit],
    width: int = 80,
    confirmation: BlastConfirmation | None = None,
) -> Text:
    """Build a Rich Text showing the CDS within the transcript and Pfam
    domains within the translated protein."""
    seq_len = transcript.length
    if seq_len == 0:
        return Text("(empty sequence)")

    label_w = 12
    suffix_w = 18
    track_w = max(width - label_w - suffix_w, 30)
    rule_w = label_w + track_w + 2

    # Find the longest ORF (putative CDS)
    best_orf = _find_longest_orf(transcript.sequence, transcript.id)

    result = Text()

    # ── Transcript track ─────────────────────────────────────────────
    result.append(f"  {transcript.id}", style="bold bright_white")
    result.append(f"  ({seq_len:,} nt)", style="dim")
    # Show CDS confirmation status
    if confirmation is not None:
        if confirmation.confirmed:
            result.append("  CONFIRMED", style="bold bright_green")
        else:
            result.append("  UNCONFIRMED", style="bold bright_red")
    result.append("\n")
    result.append("─" * rule_w + "\n", style="dim")
    result.append(_render_scale(label_w, track_w, seq_len, " nt"))

    def pos_to_col(pos: int, total: int) -> int:
        return min(int(pos / total * track_w), track_w) if total else 0

    # Transcript bar with CDS highlighted
    line = Text()
    line.append(f"{'Transcript':<{label_w}}", style="bold bright_white")
    if best_orf:
        orf_color = _FRAME_COLORS.get((best_orf.strand, best_orf.frame), "bright_cyan")
        c_start = pos_to_col(best_orf.nt_start, seq_len)
        c_end = pos_to_col(best_orf.nt_end, seq_len)
        c_end = max(c_end, c_start + 1)
        # 5' UTR
        if c_start > 0:
            line.append("░" * c_start, style="bright_black")
        # CDS
        cds_w = c_end - c_start + 1
        cds_label = "CDS"
        if cds_w >= 5:
            pad = cds_w - len(cds_label)
            pl = pad // 2
            pr = pad - pl
            line.append("█" * pl, style=orf_color)
            line.append(cds_label, style=f"bold reverse {orf_color}")
            line.append("█" * pr, style=orf_color)
        else:
            line.append("█" * cds_w, style=orf_color)
        # 3' UTR
        remaining = track_w - c_end
        if remaining > 0:
            line.append("░" * remaining, style="bright_black")
        frame_tag = f"{best_orf.strand}f{best_orf.frame}"
        line.append(f" {best_orf.aa_length} aa ({frame_tag})", style=f"dim {orf_color}")
    else:
        line.append("░" * (track_w + 1), style="bright_black")
        line.append(" no ORF found", style="dim")
    line.append("\n")
    result.append(line)

    if best_orf is None:
        result.append("\n")
        result.append("  No ORF ≥ 30 aa found.\n", style="dim italic")
        return result

    # ── Protein track with Pfam domains ──────────────────────────────
    protein_len = best_orf.aa_length
    result.append("\n")
    result.append(f"  Protein", style="bold bright_white")
    result.append(f"  ({protein_len:,} aa)\n", style="dim")
    result.append("─" * rule_w + "\n", style="dim")
    result.append(_render_scale(label_w, track_w, protein_len, " aa"))

    if hits:
        # Find hits that match this ORF
        orf_hits = [h for h in hits if h.query_name == best_orf.orf_id]
        if not orf_hits:
            orf_hits = hits  # fallback: show all

        # Protein backbone with all domains overlaid
        domain_color_map: dict[str, str] = {}
        color_idx = 0
        domain_info: list[tuple[int, int, str, str, str, float]] = []

        for h in orf_hits:
            family = h.target_name
            if family not in domain_color_map:
                domain_color_map[family] = _DOMAIN_PALETTE[color_idx % len(_DOMAIN_PALETTE)]
                color_idx += 1
            domain_info.append((
                h.ali_from, h.ali_to, family, h.accession,
                domain_color_map[family], h.evalue,
            ))

        # Combined protein track
        prot_line = Text()
        prot_line.append(f"{'Protein':<{label_w}}", style="bold bright_white")
        # Build character array for the protein track
        track = list("─" * (track_w + 1))
        # Map domain positions
        for aa_start, aa_end, family, _acc, dcolor, _ev in domain_info:
            c_start = pos_to_col(aa_start, protein_len)
            c_end = pos_to_col(aa_end, protein_len)
            c_end = max(c_end, c_start + 1)
            for c in range(c_start, min(c_end + 1, track_w + 1)):
                track[c] = "█"

        # Render with domain colors (simplified: use first domain color per position)
        # Build a color map per column
        col_color = ["bright_black"] * (track_w + 1)
        for aa_start, aa_end, _fam, _acc, dcolor, _ev in domain_info:
            c_start = pos_to_col(aa_start, protein_len)
            c_end = pos_to_col(aa_end, protein_len)
            c_end = max(c_end, c_start + 1)
            for c in range(c_start, min(c_end + 1, track_w + 1)):
                col_color[c] = dcolor

        ci = 0
        while ci < len(track):
            ch = track[ci]
            cur_color = col_color[ci] if ch == "█" else "bright_black"
            run_end = ci + 1
            while run_end < len(track) and track[run_end] == ch and (col_color[run_end] if track[run_end] == "█" else "bright_black") == cur_color:
                run_end += 1
            prot_line.append("".join(track[ci:run_end]), style=cur_color)
            ci = run_end
        prot_line.append("\n")
        result.append(prot_line)

        # Individual domain rows
        for aa_start, aa_end, family, accession, dcolor, evalue in domain_info:
            line = Text()
            line.append(" " * label_w)
            c_start = pos_to_col(aa_start, protein_len)
            c_end = pos_to_col(aa_end, protein_len)
            c_end = max(c_end, c_start + 2)
            c_end = min(c_end, track_w)

            block_w = c_end - c_start + 1
            name_display = family[:block_w - 2] if block_w >= 6 else ("▓" * block_w)
            pad_total = block_w - len(name_display)
            pad_left = pad_total // 2
            pad_right = pad_total - pad_left

            if c_start > 0:
                line.append("─" * c_start, style="bright_black")
            line.append("▓" * pad_left, style=dcolor)
            if name_display and "▓" not in name_display:
                line.append(name_display, style=f"bold reverse {dcolor}")
            else:
                line.append(name_display, style=dcolor)
            line.append("▓" * pad_right, style=dcolor)
            remaining = track_w - c_end
            if remaining > 0:
                line.append("─" * remaining, style="bright_black")
            line.append(f" {aa_start}-{aa_end} aa", style=f"dim {dcolor}")
            line.append("\n")
            result.append(line)

        # Legend
        result.append("\n")
        for family, dcolor in domain_color_map.items():
            matching = [h for h in orf_hits if h.target_name == family]
            if matching:
                h = matching[0]
                result.append(f"  ▓▓ ", style=f"bold {dcolor}")
                result.append(f"{family}", style=f"bold {dcolor}")
                result.append(f"  {h.accession}", style=f"dim {dcolor}")
                result.append(f"  E={h.evalue:.1e}  score={h.score:.1f}", style="dim")
                desc = h.description[:50] if h.description else ""
                if desc:
                    result.append(f"  {desc}", style="italic dim")
                result.append("\n")
    else:
        # No hits — just show the bare protein bar
        prot_line = Text()
        prot_line.append(f"{'Protein':<{label_w}}", style="bold bright_white")
        prot_line.append("─" * (track_w + 1), style="bright_black")
        prot_line.append("\n")
        result.append(prot_line)
        result.append("\n")
        result.append("  No Pfam domain hits.\n", style="dim italic")

    # ── BLAST confirmation details ───────────────────────────────────
    if confirmation is not None:
        result.append("\n")
        result.append("─" * rule_w + "\n", style="dim")
        if confirmation.confirmed:
            result.append("  CDS Confirmation: ", style="bold bright_white")
            result.append("CONFIRMED ", style="bold bright_green")
            result.append("via NCBI blastp\n", style="dim")
        else:
            result.append("  CDS Confirmation: ", style="bold bright_white")
            result.append("UNCONFIRMED ", style="bold bright_red")
            result.append("via NCBI blastp\n", style="dim")
        if confirmation.top_hit_name:
            result.append(f"  Top hit: ", style="dim")
            result.append(f"{confirmation.top_hit_acc}", style="bold bright_cyan")
            result.append(f"  {confirmation.top_hit_name}\n", style="dim")
        result.append(
            f"  Identity: {confirmation.identity_pct:.1f}%"
            f"  Coverage: {confirmation.coverage_pct:.1f}%"
            f"  E-value: {confirmation.evalue:.1e}\n",
            style="dim",
        )

    return result


_hmm_cpus = min(os.cpu_count() or 4, 8)

# Threading event used to abort long-running HMM scans (e.g. on app quit).
_hmm_cancel = __import__("threading").Event()


class HMMCancelled(Exception):
    """Raised when an HMM scan is aborted."""


# ── HMM database cache ──────────────────────────────────────────────────────
# Avoids reloading ~20k Pfam HMMs on every scan.

class _HMMCache:
    """Cache pressed/loaded HMMs so repeated scans skip the file-loading step."""

    def __init__(self) -> None:
        self._path: str = ""
        self._hmms: list | None = None
        self._lock = __import__("threading").Lock()

    def get(self, db_path: str) -> list:
        with self._lock:
            if self._path == db_path and self._hmms is not None:
                return self._hmms
            # Load inside the lock so concurrent callers wait instead of
            # both loading the file independently.
            _log.info("Loading HMM database %s …", db_path)
            import pyhmmer
            with pyhmmer.plan7.HMMFile(db_path) as hmm_file:
                hmms = list(hmm_file)
            self._path = db_path
            self._hmms = hmms
            _log.info("Cached %d HMM profiles from %s", len(hmms), db_path)
            return hmms

    def clear(self) -> None:
        with self._lock:
            self._path = ""
            self._hmms = None


_hmm_cache = _HMMCache()


async def hmmscan(
    sequence: str,
    seq_id: str,
    hmm_db_path: str,
    evalue_threshold: float = 1e-5,
    translate: bool = True,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[HmmerHit]:
    import pyhmmer

    if translate:
        seqs_to_search = _six_frame_proteins(sequence, seq_id)
        if not seqs_to_search:
            return []
    else:
        seqs_to_search = [(seq_id, sequence)]

    alpha = pyhmmer.easel.Alphabet.amino()

    def _run() -> list[HmmerHit]:
        digital_seqs = []
        for name, seq in seqs_to_search:
            ts = pyhmmer.easel.TextSequence(name=name, sequence=seq)
            digital_seqs.append(ts.digitize(alpha))

        hmms = _hmm_cache.get(hmm_db_path)
        total = len(hmms)
        step = max(total // 100, 1)
        results: list[HmmerHit] = []
        for i, top_hits in enumerate(pyhmmer.hmmsearch(hmms, digital_seqs, cpus=_hmm_cpus)):
            if _hmm_cancel.is_set():
                raise HMMCancelled()
            if progress_cb and (i % step == 0 or i + 1 == total):
                progress_cb(i + 1, total)
            query = top_hits.query
            for hit in top_hits:
                if hit.evalue > evalue_threshold:
                    continue
                best_dom = max(hit.domains.included, key=lambda d: d.score, default=None)
                results.append(HmmerHit(
                    target_name=str(query.name or ""),
                    accession=str(query.accession or ""),
                    query_name=str(hit.name or ""),
                    evalue=hit.evalue,
                    score=hit.score,
                    bias=hit.bias,
                    description=str(query.description or ""),
                    dom_evalue=best_dom.c_evalue if best_dom else 0.0,
                    dom_score=best_dom.score if best_dom else 0.0,
                    hmm_from=best_dom.alignment.hmm_from if best_dom else 0,
                    hmm_to=best_dom.alignment.hmm_to if best_dom else 0,
                    ali_from=best_dom.alignment.target_from if best_dom else 0,
                    ali_to=best_dom.alignment.target_to if best_dom else 0,
                ))
        results.sort(key=lambda h: h.evalue)
        return results

    return await asyncio.get_running_loop().run_in_executor(None, _run)


async def hmmsearch_all(
    transcripts: list[Transcript],
    hmm_db_path: str,
    evalue_threshold: float = 1e-5,
    translate: bool = True,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, set[str]]:
    import pyhmmer

    alpha = pyhmmer.easel.Alphabet.amino()

    def _run() -> dict[str, set[str]]:
        digital_seqs = []
        orf_to_parent: dict[str, str] = {}

        for t in transcripts:
            if translate:
                best = _find_longest_orf(t.sequence, t.id)
                if best is None:
                    continue
                ts = pyhmmer.easel.TextSequence(name=best.orf_id, sequence=best.sequence)
                digital_seqs.append(ts.digitize(alpha))
                orf_to_parent[best.orf_id] = t.id
            else:
                ts = pyhmmer.easel.TextSequence(name=t.id, sequence=t.sequence)
                digital_seqs.append(ts.digitize(alpha))
                orf_to_parent[t.id] = t.id

        if not digital_seqs:
            return {}

        hmms = _hmm_cache.get(hmm_db_path)
        total = len(hmms)
        step = max(total // 100, 1)
        mapping: dict[str, set[str]] = {}
        for i, top_hits in enumerate(pyhmmer.hmmsearch(hmms, digital_seqs, cpus=_hmm_cpus)):
            if _hmm_cancel.is_set():
                raise HMMCancelled()
            if progress_cb and (i % step == 0 or i + 1 == total):
                progress_cb(i + 1, total)
            query = top_hits.query
            pfam_name = str(query.name or "")
            pfam_acc = str(query.accession or "")

            for hit in top_hits:
                if hit.evalue > evalue_threshold:
                    continue

                orf_id = str(hit.name or "")
                parent_id = orf_to_parent.get(orf_id)
                if parent_id:
                    if parent_id not in mapping:
                        mapping[parent_id] = set()
                    mapping[parent_id].add(pfam_name.lower())
                    if pfam_acc:
                        mapping[parent_id].add(pfam_acc.lower())

        return mapping

    return await asyncio.get_running_loop().run_in_executor(None, _run)


# ══════════════════════════════════════════════════════════════════════════════
# NCBI BLAST CDS confirmation
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class BlastConfirmation:
    """Result of confirming a putative CDS via NCBI BLAST."""
    confirmed: bool
    top_hit_name: str = ""
    top_hit_acc: str = ""
    identity_pct: float = 0.0
    coverage_pct: float = 0.0
    evalue: float = 999.0
    query_length: int = 0
    alignment_length: int = 0


# ══════════════════════════════════════════════════════════════════════════════
# Pfam database downloader
# ══════════════════════════════════════════════════════════════════════════════

_PFAM_BASE_URL = "https://ftp.ebi.ac.uk/pub/databases/Pfam/current_release"
_PFAM_HMM_URL = f"{_PFAM_BASE_URL}/Pfam-A.hmm.gz"
_PFAM_CLANS_URL = f"{_PFAM_BASE_URL}/Pfam-A.clans.tsv.gz"
_PFAM_DEFAULT_DIR = Path.home() / ".scriptoscope" / "pfam"


def _download_file(
    url: str, dest: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Download a file from *url* to *dest* with optional progress callback."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "ScriptoScope"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)


def _decompress_gz(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as gz_in, open(dest, "wb") as out:
        shutil.copyfileobj(gz_in, out)


def _press_hmm(hmm_path: Path) -> None:
    """Press an HMM file using pyhmmer for fast access."""
    import pyhmmer
    with pyhmmer.plan7.HMMFile(str(hmm_path)) as hmm_file:
        hmms = list(hmm_file)
    pyhmmer.hmmpress(hmms, str(hmm_path))


def load_pfam_descriptions(clans_tsv: Path) -> dict[str, tuple[str, str]]:
    """Parse Pfam-A.clans.tsv → {accession: (name, description), name: (name, description)}."""
    mapping: dict[str, tuple[str, str]] = {}
    if not clans_tsv.exists():
        return mapping
    with open(clans_tsv, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            acc, _clan_id, _clan_name, name, desc = parts[0], parts[1], parts[2], parts[3], parts[4]
            entry = (name, desc)
            mapping[acc.lower()] = entry
            mapping[name.lower()] = entry
    return mapping


async def download_pfam(
    dest_dir: Path = _PFAM_DEFAULT_DIR,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, tuple[str, str]]]:
    """Download, decompress, and press Pfam-A.  Returns (hmm_path, descriptions)."""
    loop = asyncio.get_running_loop()
    dest_dir.mkdir(parents=True, exist_ok=True)

    hmm_gz = dest_dir / "Pfam-A.hmm.gz"
    hmm_path = dest_dir / "Pfam-A.hmm"
    clans_gz = dest_dir / "Pfam-A.clans.tsv.gz"
    clans_tsv = dest_dir / "Pfam-A.clans.tsv"

    def _do_download() -> None:
        # Download clans TSV (small, ~500KB)
        if progress_cb:
            progress_cb("Downloading Pfam annotations (Pfam-A.clans.tsv.gz)…")
        _download_file(_PFAM_CLANS_URL, clans_gz)
        _decompress_gz(clans_gz, clans_tsv)
        clans_gz.unlink(missing_ok=True)

        # Download HMM database (~367MB)
        def _hmm_progress(downloaded: int, total: int) -> None:
            if progress_cb and total > 0:
                pct = downloaded * 100 // total
                mb = downloaded / 1_048_576
                total_mb = total / 1_048_576
                progress_cb(f"Downloading Pfam-A.hmm.gz… {mb:.0f}/{total_mb:.0f} MB ({pct}%)")

        if progress_cb:
            progress_cb("Downloading Pfam-A.hmm.gz (≈367 MB)…")
        _download_file(_PFAM_HMM_URL, hmm_gz, progress_cb=_hmm_progress)

        # Decompress
        if progress_cb:
            progress_cb("Decompressing Pfam-A.hmm.gz…")
        _decompress_gz(hmm_gz, hmm_path)
        hmm_gz.unlink(missing_ok=True)

        # Press for fast access
        if progress_cb:
            progress_cb("Pressing HMM database (building indices)…")
        _press_hmm(hmm_path)

    await loop.run_in_executor(None, _do_download)

    descriptions = await loop.run_in_executor(None, load_pfam_descriptions, clans_tsv)
    return str(hmm_path), descriptions


def pfam_db_exists(dest_dir: Path = _PFAM_DEFAULT_DIR) -> str | None:
    """Return the path to the local Pfam HMM if it exists, else None."""
    hmm_path = dest_dir / "Pfam-A.hmm"
    if hmm_path.exists():
        return str(hmm_path)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Sequence viewer widget
# ══════════════════════════════════════════════════════════════════════════════

_BASE_COLORS = {
    "A": "bold green",
    "T": "bold red",
    "U": "bold red",
    "G": "bold yellow",
    "C": "bold cyan",
    "N": "dim white",
}
_MAX_DISPLAY_BASES = 10_000


def colorize_sequence(seq: str, width: int = 60) -> Text:
    truncated = len(seq) > _MAX_DISPLAY_BASES
    display_seq = seq[:_MAX_DISPLAY_BASES] if truncated else seq

    result = Text(no_wrap=False)
    for i in range(0, len(display_seq), width):
        chunk = display_seq[i : i + width]
        result.append(f"{i+1:>8}  ", style="dim")
        run_base = chunk[0].upper()
        run_text = chunk[0]
        for base in chunk[1:]:
            upper = base.upper()
            if upper == run_base:
                run_text += base
            else:
                result.append(run_text, style=_BASE_COLORS.get(run_base, "white"))
                run_base = upper
                run_text = base
        result.append(run_text, style=_BASE_COLORS.get(run_base, "white"))
        result.append("\n")

    if truncated:
        result.append(
            f"\n  … {len(seq) - _MAX_DISPLAY_BASES:,} more bases not shown\n",
            style="dim italic",
        )
    return result


def colorize_sequence_annotated(
    seq: str,
    orf: ORFCoord | None = None,
    hits: list[HmmerHit] | None = None,
    width: int = 60,
) -> Text:
    """Render DNA with CDS amino-acid translation and Pfam domain tracks."""
    truncated = len(seq) > _MAX_DISPLAY_BASES
    display_seq = seq[:_MAX_DISPLAY_BASES] if truncated else seq
    prefix_w = 10  # "  12345  " — 8-digit number + 2 spaces

    # Pre-compute CDS and Pfam annotation arrays over the full display range
    # aa_at[i] = amino acid character at nucleotide position i (or None)
    # aa_color[i] = style for that amino acid
    # pfam_label[i] = character to show on the Pfam track (or None)
    # pfam_color[i] = style for that Pfam character
    n = len(display_seq)
    aa_at: list[str | None] = [None] * n
    aa_color: list[str] = [""] * n
    pfam_label: list[str | None] = [None] * n
    pfam_color: list[str] = [""] * n

    if orf and orf.strand == "+":
        cds_color = _FRAME_COLORS.get((orf.strand, orf.frame), "bright_cyan")
        for aa_idx in range(orf.aa_length):
            codon_start = orf.nt_start + aa_idx * 3
            if codon_start + 2 >= n:
                break
            aa_char = orf.sequence[aa_idx]
            # Place the amino acid letter at the middle base of each codon
            mid = codon_start + 1
            aa_at[mid] = aa_char
            aa_color[mid] = cds_color
            # Mark flanking bases with dots to show codon boundaries
            aa_at[codon_start] = "·"
            aa_color[codon_start] = "dim"
            if codon_start + 2 < n:
                aa_at[codon_start + 2] = "·"
                aa_color[codon_start + 2] = "dim"

        # Build Pfam domain annotations in nucleotide space
        if hits:
            domain_colors: dict[str, str] = {}
            cidx = 0
            for h in hits:
                if h.target_name not in domain_colors:
                    domain_colors[h.target_name] = _DOMAIN_PALETTE[cidx % len(_DOMAIN_PALETTE)]
                    cidx += 1

            for h in hits:
                dcolor = domain_colors[h.target_name]
                # ali_from/ali_to are 1-based amino acid positions
                aa_start = h.ali_from - 1  # 0-based
                aa_end = h.ali_to          # exclusive
                nt_dom_start = orf.nt_start + aa_start * 3
                nt_dom_end = orf.nt_start + aa_end * 3
                name = h.target_name
                dom_nt_len = nt_dom_end - nt_dom_start
                # Center the domain name within the span
                if dom_nt_len >= len(name) + 2:
                    pad = dom_nt_len - len(name)
                    pad_l = pad // 2
                    for j in range(dom_nt_len):
                        pos = nt_dom_start + j
                        if pos >= n:
                            break
                        if j < pad_l or j >= pad_l + len(name):
                            pfam_label[pos] = "━"
                            pfam_color[pos] = dcolor
                        else:
                            pfam_label[pos] = name[j - pad_l]
                            pfam_color[pos] = f"bold {dcolor}"
                else:
                    for j in range(dom_nt_len):
                        pos = nt_dom_start + j
                        if pos >= n:
                            break
                        pfam_label[pos] = "━"
                        pfam_color[pos] = dcolor

    result = Text(no_wrap=False)

    # Header: CDS and Pfam summary
    if orf:
        cds_color = _FRAME_COLORS.get((orf.strand, orf.frame), "bright_cyan")
        result.append(f"  CDS: ", style="dim")
        result.append(f"{orf.nt_start+1}–{orf.nt_end}", style=f"bold {cds_color}")
        result.append(f" ({orf.aa_length} aa, {orf.strand}f{orf.frame})", style=f"dim {cds_color}")
        if hits:
            result.append("  Pfam: ", style="dim")
            domain_colors_hdr: dict[str, str] = {}
            cidx = 0
            for h in hits:
                if h.target_name not in domain_colors_hdr:
                    domain_colors_hdr[h.target_name] = _DOMAIN_PALETTE[cidx % len(_DOMAIN_PALETTE)]
                    cidx += 1
            for fam, dcol in domain_colors_hdr.items():
                result.append(f"━━ {fam} ", style=f"bold {dcol}")
        result.append("\n\n")

    for i in range(0, len(display_seq), width):
        chunk = display_seq[i : i + width]
        chunk_len = len(chunk)

        # ── DNA line ──
        result.append(f"{i+1:>8}  ", style="dim")
        run_base = chunk[0].upper()
        run_text = chunk[0]
        for base in chunk[1:]:
            upper = base.upper()
            if upper == run_base:
                run_text += base
            else:
                result.append(run_text, style=_BASE_COLORS.get(run_base, "white"))
                run_base = upper
                run_text = base
        result.append(run_text, style=_BASE_COLORS.get(run_base, "white"))
        result.append("\n")

        # ── Amino acid translation line (only if CDS overlaps this chunk) ──
        if orf and orf.strand == "+":
            has_aa = any(aa_at[i + j] is not None for j in range(chunk_len) if i + j < n)
            if has_aa:
                result.append(" " * prefix_w)
                for j in range(chunk_len):
                    pos = i + j
                    if pos < n and aa_at[pos] is not None:
                        ch = aa_at[pos]
                        if ch == "·":
                            result.append(ch, style="dim")
                        else:
                            result.append(ch, style=f"bold {aa_color[pos]}")
                    else:
                        result.append(" ")
                result.append("\n")

        # ── Pfam domain line (only if domains overlap this chunk) ──
        if hits and orf and orf.strand == "+":
            has_pfam = any(pfam_label[i + j] is not None for j in range(chunk_len) if i + j < n)
            if has_pfam:
                result.append(" " * prefix_w)
                for j in range(chunk_len):
                    pos = i + j
                    if pos < n and pfam_label[pos] is not None:
                        result.append(pfam_label[pos], style=pfam_color[pos])
                    else:
                        result.append(" ")
                result.append("\n")

    if truncated:
        result.append(
            f"\n  … {len(seq) - _MAX_DISPLAY_BASES:,} more bases not shown\n",
            style="dim italic",
        )
    return result


class SequenceViewer(ScrollableContainer):
    DEFAULT_CSS = """
    SequenceViewer { height: 1fr; }
    SequenceViewer #seq-info { background: $surface; padding: 0 1; height: auto; }
    SequenceViewer #seq-body { padding: 0 1; height: auto; }
    """

    transcript: reactive[Transcript | None] = reactive(None)

    def watch_transcript(self, t: Transcript | None) -> None:
        if t:
            self.show_transcript(t)
        else:
            self.query_one("#seq-info", Static).update("Select a transcript from the list.")
            self.query_one("#seq-body", Static).update("")

    def compose(self) -> ComposeResult:
        yield Static("Select a transcript from the list.", id="seq-info")
        yield Static("", id="seq-body")

    def refresh_annotations(self) -> None:
        """Re-render the sequence with current scan data from the HmmerPanel."""
        t = self.transcript
        if t:
            self.show_transcript(t)

    def show_transcript(self, t: Transcript) -> None:
        info = self.query_one("#seq-info", Static)
        body = self.query_one("#seq-body", Static)
        try:
            counts = t.nucleotide_counts()
            grid = Table.grid(padding=(0, 2))
            grid.add_column(style="bold cyan", no_wrap=True)
            grid.add_column(no_wrap=True)
            grid.add_row("ID", t.id)
            grid.add_row("Description", t.description or "(none)")
            grid.add_row("Length", f"{t.length:,} bp")
            grid.add_row("GC Content", f"{t.gc_content:.1f}%")
            grid.add_row(
                "Composition",
                (
                    f"[bold green]A[/]:{counts['A']}  "
                    f"[bold cyan]C[/]:{counts['C']}  "
                    f"[bold yellow]G[/]:{counts['G']}  "
                    f"[bold red]T[/]:{counts['T']}  "
                    f"[dim]N[/]:{counts['N']}"
                ),
            )
            info.update(grid)

            # Check if HMMER scan data is available for this transcript
            orf = None
            hits: list[HmmerHit] = []
            try:
                hmmer = self.app.query_one("#hmmer-panel")
                scan_cache = getattr(hmmer, "_scan_cache", {})
                hits = scan_cache.get(t.id, [])
                if hits or t.id in scan_cache:
                    orf = _find_longest_orf(t.sequence, t.id)
            except Exception:
                pass

            if orf or hits:
                body.update(colorize_sequence_annotated(
                    t.sequence, orf=orf, hits=hits,
                ))
            else:
                body.update(colorize_sequence(t.sequence))
        except Exception as exc:
            _log.exception("SequenceViewer.show_transcript failed: %s", exc)
            info.update(f"[red]Error: {exc}[/]")
            body.update("")


# ══════════════════════════════════════════════════════════════════════════════
# Statistics panel widget
# ══════════════════════════════════════════════════════════════════════════════

_BUCKET_LABELS = ["<200 bp", "200–500 bp", "500–1k bp", "1k–2k bp", "2k–5k bp", ">5k bp"]


def _compute_stats(transcripts: list[Transcript]) -> dict:
    lengths = sorted(t.length for t in transcripts)
    n = len(lengths)
    if n == 0:
        return {
            "n": 0, "total_bases": 0,
            "shortest": 0, "longest": 0,
            "mean_len": 0, "median_len": 0,
            "n50": 0, "mean_gc": 0,
            "bucket_counts": [0] * 6,
        }
    total_bases = sum(lengths)
    mean_len = total_bases / n
    median_len = lengths[n // 2]

    half = total_bases / 2
    cumulative = 0
    n50 = 0
    for length in reversed(lengths):
        cumulative += length
        if cumulative >= half:
            n50 = length
            break

    mean_gc = sum(t.gc_content for t in transcripts) / n

    bucket_counts = [0] * 6
    for length in lengths:
        if length < 200:
            bucket_counts[0] += 1
        elif length < 500:
            bucket_counts[1] += 1
        elif length < 1000:
            bucket_counts[2] += 1
        elif length < 2000:
            bucket_counts[3] += 1
        elif length < 5000:
            bucket_counts[4] += 1
        else:
            bucket_counts[5] += 1

    return {
        "n": n, "total_bases": total_bases,
        "shortest": lengths[0], "longest": lengths[-1],
        "mean_len": mean_len, "median_len": median_len,
        "n50": n50, "mean_gc": mean_gc,
        "bucket_counts": bucket_counts,
    }


class StatsPanel(ScrollableContainer):
    DEFAULT_CSS = """
    StatsPanel { height: 1fr; padding: 1 2; }
    StatsPanel Static { height: auto; }
    """

    transcript: reactive[Transcript | None] = reactive(None)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._global_stats: dict | None = None
        self._fasta_path: str = ""

    def compose(self) -> ComposeResult:
        yield Static("No transcriptome loaded.", id="stats-content")

    def on_mount(self) -> None:
        self._update_display()

    def watch_transcript(self, t: Transcript | None) -> None:
        self._update_display()

    def render_stats(self, s: dict, fasta_path: str) -> None:
        self._global_stats = s
        self._fasta_path = fasta_path
        self._update_display()

    def _update_display(self) -> None:
        try:
            content = self.query_one("#stats-content", Static)
        except Exception:
            return

        if not self._global_stats:
            content.update("No transcriptome loaded.")
            return

        elements = []

        # ── Selected Transcript ───────────────────────────────────────────────
        t = self.transcript
        if t:
            grid = Table.grid(padding=(0, 3))
            grid.add_column(style="bold cyan", no_wrap=True)
            grid.add_column(no_wrap=True)
            grid.title = "Selected Transcript"
            grid.title_style = "bold magenta"
            grid.add_row("ID", t.id)
            grid.add_row("Length", f"{t.length:,} bp")
            grid.add_row("GC Content", f"{t.gc_content:.1f}%")
            elements.append(grid)
            elements.append(Text(""))

        # ── Global Statistics ─────────────────────────────────────────────────
        s = self._global_stats
        n = s["n"]
        summary = Table.grid(padding=(0, 3))
        summary.add_column(style="bold cyan", no_wrap=True)
        summary.add_column(no_wrap=True)
        summary.title = "Global Transcriptome Statistics"
        summary.title_style = "bold magenta"
        summary.add_row("File", self._fasta_path)
        summary.add_row("Total transcripts", f"{n:,}")
        summary.add_row("Total bases", f"{s['total_bases']:,} bp")
        summary.add_row("Shortest", f"{s['shortest']:,} bp")
        summary.add_row("Longest", f"{s['longest']:,} bp")
        summary.add_row("Mean length", f"{s['mean_len']:,.1f} bp")
        summary.add_row("Median length", f"{s['median_len']:,} bp")
        summary.add_row("N50", f"{s['n50']:,} bp")
        summary.add_row("Mean GC content", f"{s['mean_gc']:.1f}%")
        elements.append(summary)
        elements.append(Text(""))

        dist = Table(
            "Length range", "Count", "%",
            title="Length Distribution",
            title_style="bold magenta",
            header_style="bold magenta",
        )
        for label, count in zip(_BUCKET_LABELS, s["bucket_counts"]):
            dist.add_row(label, f"{count:,}", f"{count / n * 100:.1f}%")
        elements.append(dist)

        content.update(Group(*elements))



# ══════════════════════════════════════════════════════════════════════════════
# File browser modal
# ══════════════════════════════════════════════════════════════════════════════

_FASTA_EXTENSIONS = {".fasta", ".fa", ".fna", ".ffn", ".faa", ".frn", ".fas"}


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _list_dir(path: Path) -> tuple[list[Path], list[Path]]:
    dirs, files = [], []
    try:
        for entry in path.iterdir():
            try:
                if entry.is_dir():
                    dirs.append(entry)
                elif entry.is_file():
                    files.append(entry)
            except PermissionError:
                pass
    except PermissionError:
        pass
    dirs.sort(key=lambda p: p.name.lower())
    files.sort(key=lambda p: p.name.lower())
    return dirs, files


class FileBrowserModal(ModalScreen[str | None]):
    DEFAULT_CSS = """
    FileBrowserModal { align: center middle; }
    FileBrowserModal #fb-dialog {
        width: 90; height: 36;
        border: thick $primary 80%; background: $surface;
    }
    FileBrowserModal #fb-title {
        height: 1; background: $primary; color: $text;
        text-align: center; text-style: bold; padding: 0 1;
    }
    FileBrowserModal #fb-path-row {
        height: 3; padding: 0 1; align: left middle;
        background: $surface-darken-1;
    }
    FileBrowserModal #fb-path-label { width: 7; content-align: right middle; color: $text-muted; }
    FileBrowserModal #fb-filter-row {
        height: 3; padding: 0 1; align: left middle;
        background: $surface-darken-1;
    }
    FileBrowserModal #fb-filter-label { width: 7; content-align: right middle; color: $text-muted; }
    FileBrowserModal #fb-table { height: 1fr; margin: 0 1; }
    FileBrowserModal #fb-status {
        height: 1; padding: 0 1; color: $text-muted; background: $surface-darken-1;
    }
    FileBrowserModal #fb-buttons {
        height: 3; padding: 0 1; align: right middle; background: $surface-darken-1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("backspace", "go_up", "Parent dir", show=True),
    ]

    def __init__(self, start_path: str | None = None) -> None:
        super().__init__()
        candidate = Path(start_path).expanduser().resolve() if start_path else Path.home()
        self._cwd = candidate if candidate.is_dir() else candidate.parent
        self._filter = ""
        self._row_paths: list[Path | None] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="fb-dialog"):
            yield Label(" Open FASTA File", id="fb-title")
            with Horizontal(id="fb-path-row"):
                yield Label("Path:", id="fb-path-label")
                yield Input(str(self._cwd), id="fb-path-input")
            with Horizontal(id="fb-filter-row"):
                yield Label("Filter:", id="fb-filter-label")
                yield Input(placeholder="type to filter files…", id="fb-filter-input")
            yield DataTable(id="fb-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="fb-status")
            with Horizontal(id="fb-buttons"):
                yield Button("Cancel", id="fb-cancel")
                yield Button("Open", id="fb-open", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#fb-table", DataTable).add_columns("  Name", "Size", "Type")
        self._navigate(self._cwd)

    def _navigate(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_dir():
            path = path.parent
        self._cwd = path
        self.query_one("#fb-path-input", Input).value = str(path)

        dirs, all_files = _list_dir(path)
        flt = self._filter.lower()
        files = [f for f in all_files if flt in f.name.lower()] if flt else all_files

        table = self.query_one("#fb-table", DataTable)
        table.clear()
        self._row_paths = []

        if path.parent != path:
            table.add_row("  [bold cyan]..[/]", "", "[dim]up[/]")
            self._row_paths.append(None)

        for d in dirs:
            table.add_row(f"  [bold cyan]{d.name}/[/]", "", "[cyan]dir[/]")
            self._row_paths.append(d)

        fasta_count = 0
        for f in files:
            is_fasta = f.suffix.lower() in _FASTA_EXTENSIONS
            if is_fasta:
                fasta_count += 1
            style = "bold green" if is_fasta else "white"
            label = "FASTA" if is_fasta else "file"
            try:
                size = _fmt_size(f.stat().st_size)
            except OSError:
                size = "?"
            table.add_row(f"  [{style}]{f.name}[/]", size, f"[{style}]{label}[/]")
            self._row_paths.append(f)

        self._set_status(
            f"{len(dirs)} dirs   {len(files)} files"
            + (f"   ({fasta_count} FASTA)" if fasta_count else "")
        )
        self.query_one("#fb-open", Button).disabled = True
        if table.row_count:
            table.move_cursor(row=0)

    def _path_at(self, row: int) -> Path | None:
        return self._row_paths[row] if 0 <= row < len(self._row_paths) else None

    @on(DataTable.RowHighlighted, "#fb-table")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        row = event.cursor_row
        path = self._path_at(row)
        is_file = path is not None and path.is_file()
        self.query_one("#fb-open", Button).disabled = not is_file
        if is_file:
            self._set_status(str(path))
        elif path is None and self._cwd.parent != self._cwd:
            self._set_status(str(self._cwd.parent))
        elif path is not None:
            self._set_status(str(path))

    @on(DataTable.RowSelected, "#fb-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        path = self._path_at(row)
        if path is None:
            self._navigate(self._cwd.parent)
        elif path.is_dir():
            self._navigate(path)
        elif path.is_file():
            self.dismiss(str(path))

    @on(Input.Submitted, "#fb-path-input")
    def path_submitted(self, event: Input.Submitted) -> None:
        target = Path(event.value.strip()).expanduser().resolve()
        if target.is_dir():
            self._navigate(target)
        elif target.is_file():
            self.dismiss(str(target))
        else:
            self._set_status(f"[red]Not found: {target}[/]")

    @on(Input.Changed, "#fb-filter-input")
    def filter_changed(self, event: Input.Changed) -> None:
        self._filter = event.value.strip()
        self._navigate(self._cwd)

    @on(Button.Pressed, "#fb-open")
    def open_pressed(self) -> None:
        table = self.query_one("#fb-table", DataTable)
        path = self._path_at(table.cursor_row)
        if path and path.is_file():
            self.dismiss(str(path))

    @on(Button.Pressed, "#fb-cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_go_up(self) -> None:
        self._navigate(self._cwd.parent)

    def _set_status(self, msg: str) -> None:
        self.query_one("#fb-status", Static).update(msg)


# ══════════════════════════════════════════════════════════════════════════════
# BLAST panel widget
# ══════════════════════════════════════════════════════════════════════════════

_BLAST_PROGRAMS = ["blastn", "tblastn", "blastx", "blastp", "tblastx"]


def _detect_seq_type(seq: str) -> str:
    """Return 'dna' if the sequence looks like nucleotides, else 'protein'."""
    upper = set(seq.upper()) - set(" \t\n\r0123456789>")
    dna_bases = {"A", "C", "G", "T", "U", "N", "R", "Y", "S", "W", "K", "M",
                 "B", "D", "H", "V"}
    if upper and upper <= dna_bases:
        return "dna"
    return "protein"


def _clean_seq(raw: str) -> str:
    """Strip FASTA headers, whitespace, and digits from pasted sequence text."""
    lines = raw.splitlines()
    parts: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        # Remove spaces, digits, line numbers
        cleaned = "".join(ch for ch in stripped if ch.isalpha() or ch == "*")
        if cleaned:
            parts.append(cleaned)
    return "".join(parts)


class BlastPanel(Vertical):
    DEFAULT_CSS = """
    BlastPanel { height: 1fr; }
    BlastPanel .blast-form { height: auto; padding: 1 2; background: $surface; }
    BlastPanel .blast-row { height: 3; align: left middle; }
    BlastPanel .blast-label { width: 18; content-align: right middle; padding-right: 1; }
    BlastPanel #blast-query-area { height: auto; padding: 0 2; background: $surface; }
    BlastPanel #blast-query-input {
        height: 6; margin: 0 0 1 0;
    }
    BlastPanel #blast-query-type { height: 1; padding: 0 2; color: $text-muted; }
    BlastPanel #blast-status { height: 2; padding: 0 2; color: $text-muted; }
    BlastPanel #blast-loading { display: none; height: 3; }
    BlastPanel #blast-loading.running { display: block; }
    BlastPanel #blast-results-area { height: 1fr; padding: 1; }
    BlastPanel DataTable { height: 1fr; }
    BlastPanel RadioSet { layout: horizontal; height: 3; }
    BlastPanel RadioButton { width: auto; margin: 0 2 0 0; }
    """

    transcript: reactive[Transcript | None] = reactive(None)

    def compose(self) -> ComposeResult:
        with Vertical(classes="blast-form"):
            with Horizontal(classes="blast-row"):
                yield Label("Query:", classes="blast-label")
                with RadioSet(id="blast-query-source"):
                    yield RadioButton("Selected transcript", value=True, id="blast-src-transcript")
                    yield RadioButton("Custom sequence", id="blast-src-custom")
            with Vertical(id="blast-query-area"):
                yield TextArea(
                    "",
                    id="blast-query-input",
                    language=None,
                    show_line_numbers=False,
                    soft_wrap=True,
                )
                yield Static("Paste DNA or protein sequence (FASTA headers are stripped automatically)", id="blast-query-type")
            with Horizontal(classes="blast-row"):
                yield Label("DB path:", classes="blast-label")
                yield Input(placeholder="/path/to/blast/db", id="blast-db-input")
            with Horizontal(classes="blast-row"):
                yield Label("Program:", classes="blast-label")
                yield Select(
                    [(p, p) for p in _BLAST_PROGRAMS],
                    id="blast-program", value="blastn",
                )
                yield Label("E-value:", classes="blast-label")
                yield Input(value="1e-5", id="blast-evalue")
                yield Label("Max hits:", classes="blast-label")
                yield Input(value="50", id="blast-maxhits")
            with Horizontal(classes="blast-row"):
                yield Button("Run BLAST", id="blast-run", variant="primary")
                yield Button("Use transcriptome as DB", id="blast-use-transcriptome")
                yield Static(id="blast-status")
        yield LoadingIndicator(id="blast-loading")
        with VerticalScroll(id="blast-results-area"):
            yield DataTable(id="blast-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#blast-table", DataTable).add_columns(
            "Subject", "% ID", "Aln Len", "E-value", "Bit Score",
            "Q Start", "Q End", "S Start", "S End",
        )
        if not blast_available():
            self.query_one("#blast-status", Static).update(
                "[yellow]BLAST+ not found in PATH — install NCBI BLAST+[/]"
            )
        # Start with custom query area hidden
        self.query_one("#blast-query-area").display = False

    def set_transcriptome_db(self, db_path: str) -> None:
        self.query_one("#blast-db-input", Input).value = db_path

    @on(RadioSet.Changed, "#blast-query-source")
    def _query_source_changed(self, event: RadioSet.Changed) -> None:
        use_custom = event.index == 1
        self.query_one("#blast-query-area").display = use_custom

    @on(TextArea.Changed, "#blast-query-input")
    def _query_text_changed(self, event: TextArea.Changed) -> None:
        raw = event.text_area.text
        cleaned = _clean_seq(raw)
        if not cleaned:
            self.query_one("#blast-query-type", Static).update(
                "Paste DNA or protein sequence (FASTA headers are stripped automatically)"
            )
            return
        seq_type = _detect_seq_type(cleaned)
        label = f"[green]Detected: {seq_type.upper()}[/] — {len(cleaned):,} residues"
        if seq_type == "dna":
            suggested = "blastn"
            label += "  [dim](suggested: blastn)[/]"
        else:
            suggested = "tblastn"
            label += "  [dim](suggested: tblastn for searching nucleotide DB)[/]"
        self.query_one("#blast-query-type", Static).update(label)
        self.query_one("#blast-program", Select).value = suggested

    @on(Button.Pressed, "#blast-use-transcriptome")
    def use_transcriptome_as_db(self) -> None:
        db = getattr(self.app, "_blast_db", "")
        if db:
            self.query_one("#blast-db-input", Input).value = db
            self._set_status(f"[green]DB set to transcriptome: {db}[/]")
        else:
            self._set_status("[yellow]No BLAST DB built yet — use Ctrl+D.[/]")

    def _get_query(self) -> tuple[str, str] | None:
        """Return (query_id, query_seq) or None on error."""
        radio = self.query_one("#blast-query-source", RadioSet)
        use_custom = radio.pressed_index == 1

        if use_custom:
            raw = self.query_one("#blast-query-input", TextArea).text
            cleaned = _clean_seq(raw)
            if not cleaned:
                self._set_status("[yellow]Paste a sequence into the query box.[/]")
                return None
            return ("custom_query", cleaned)
        else:
            t = self.transcript
            if t is None:
                self._set_status("[yellow]Select a transcript first.[/]")
                return None
            return (t.id, t.sequence)

    @on(Button.Pressed, "#blast-run")
    def run_blast(self) -> None:
        query = self._get_query()
        if query is None:
            return
        query_id, query_seq = query
        db = self.query_one("#blast-db-input", Input).value.strip()
        if not db:
            self._set_status("[yellow]Enter a BLAST database path.[/]")
            return
        program_val = self.query_one("#blast-program", Select).value
        if program_val is Select.BLANK:
            self._set_status("[yellow]Select a BLAST program.[/]")
            return
        evalue_str = self.query_one("#blast-evalue", Input).value.strip()
        maxhits_str = self.query_one("#blast-maxhits", Input).value.strip()
        try:
            evalue = float(evalue_str)
            maxhits = int(maxhits_str)
        except ValueError:
            self._set_status("[red]Invalid e-value or max hits.[/]")
            return
        self._run_blast_worker(query_id, query_seq, db, str(program_val), evalue, maxhits)

    @work(exclusive=True, thread=False)
    async def _run_blast_worker(
        self, query_id: str, query_seq: str, db: str,
        program: str, evalue: float, maxhits: int,
    ) -> None:
        loading = self.query_one("#blast-loading", LoadingIndicator)
        loading.add_class("running")
        self._set_status(f"Running {program}…")
        table = self.query_one("#blast-table", DataTable)
        table.clear()
        try:
            hits, _ = await local_blast(
                query_seq=query_seq, query_id=query_id, db_path=db,
                program=program, evalue=evalue, max_hits=maxhits,
            )
            for i, h in enumerate(hits):
                table.add_row(
                    h.subject_id[:40], f"{h.pct_identity:.1f}",
                    str(h.alignment_length), f"{h.evalue:.2e}", f"{h.bit_score:.1f}",
                    str(h.query_start), str(h.query_end),
                    str(h.subject_start), str(h.subject_end),
                    key=h.subject_id,
                )
            self._set_status(f"[green]{len(hits)} hits found.[/]")
        except RuntimeError as exc:
            self._set_status(f"[red]Error: {exc}[/]")
        finally:
            loading.remove_class("running")

    def _set_status(self, msg: str) -> None:
        self.query_one("#blast-status", Static).update(msg)


# ══════════════════════════════════════════════════════════════════════════════
# HMMER panel widget
# ══════════════════════════════════════════════════════════════════════════════

class HmmerPanel(Vertical):
    DEFAULT_CSS = """
    HmmerPanel { height: 1fr; }
    HmmerPanel .hmmer-form { height: auto; padding: 1 2; background: $surface; }
    HmmerPanel .hmmer-row { height: 3; align: left middle; }
    HmmerPanel .hmmer-label { width: 20; content-align: right middle; padding-right: 1; }
    HmmerPanel #hmmer-db-input { width: 1fr; }
    HmmerPanel #hmmer-download-pfam { min-width: 18; }
    HmmerPanel #hmmer-status { height: 2; padding: 0 2; color: $text-muted; }
    HmmerPanel #hmmer-progress { display: none; height: 2; padding: 0 2; }
    HmmerPanel #hmmer-progress.running { display: block; }
    HmmerPanel #hmmer-loading { display: none; height: 3; }
    HmmerPanel #hmmer-loading.running { display: block; }
    HmmerPanel #hmmer-results-area { height: 1fr; padding: 1; }
    HmmerPanel #hmmer-diagram { height: auto; padding: 1 0; }
    HmmerPanel DataTable { height: auto; max-height: 50%; }
    """

    transcript: reactive[Transcript | None] = reactive(None)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pfam_desc: dict[str, tuple[str, str]] = {}
        self._scan_cache: dict[str, list[HmmerHit]] = {}
        self._confirm_cache: dict[str, BlastConfirmation] = {}

    def watch_transcript(self, t: Transcript | None) -> None:
        """Show cached results when transcript changes, otherwise clear."""
        try:
            table = self.query_one("#hmmer-table", DataTable)
            table.clear()
            diagram = self.query_one("#hmmer-diagram", Static)

            if t is None:
                diagram.update("")
                self._set_status("")
                return

            if t.id in self._scan_cache:
                hits = self._scan_cache[t.id]
                self._display_hits(t, hits)
                conf = self._confirm_cache.get(t.id)
                if conf:
                    tag = "CONFIRMED" if conf.confirmed else "UNCONFIRMED"
                    color = "green" if conf.confirmed else "yellow"
                    self._set_status(f"[green]{len(hits)} Pfam hits (cached).[/] [{color}]CDS {tag}[/{color}]")
                else:
                    self._set_status(f"[green]{len(hits)} domain hits (cached).[/]")
            else:
                diagram.update("")
                self._set_status("")
        except Exception:
            pass

    def _display_hits(self, t: Transcript, hits: list[HmmerHit]) -> None:
        """Populate table and diagram from a list of hits."""
        table = self.query_one("#hmmer-table", DataTable)
        table.clear()
        for h in hits:
            desc = self._lookup_desc(h)
            table.add_row(
                h.target_name[:30], h.accession[:12],
                f"{h.evalue:.2e}", f"{h.score:.1f}", f"{h.bias:.1f}",
                str(h.hmm_from), str(h.hmm_to),
                str(h.ali_from), str(h.ali_to),
                desc[:60],
            )
        try:
            w = self.query_one("#hmmer-results-area").size.width - 2
            conf = self._confirm_cache.get(t.id)
            diagram = render_orf_diagram(t, hits, width=max(w, 60), confirmation=conf)
            self.query_one("#hmmer-diagram", Static).update(diagram)
        except Exception as exc:
            _log.exception("Diagram render failed: %s", exc)
        # Refresh Sequence tab so annotations appear on the DNA view
        try:
            sv = self.app.query_one("#seq-viewer")
            if hasattr(sv, "refresh_annotations"):
                sv.refresh_annotations()
        except Exception:
            pass

    def set_db_path(self, db_path: str) -> None:
        self.query_one("#hmmer-db-input", Input).value = db_path

    def _lookup_desc(self, hit: HmmerHit) -> str:
        """Look up a Pfam family description from the clans data."""
        if self._pfam_desc:
            entry = self._pfam_desc.get(hit.accession.lower())
            if not entry:
                entry = self._pfam_desc.get(hit.target_name.lower())
            if entry:
                return entry[1]
        return hit.description

    def compose(self) -> ComposeResult:
        with Vertical(classes="hmmer-form"):
            with Horizontal(classes="hmmer-row"):
                yield Label("HMM DB (.h3m/.hmm):", classes="hmmer-label")
                yield Input(placeholder="/path/to/Pfam-A.hmm", id="hmmer-db-input")
                yield Button("Download Pfam", id="hmmer-download-pfam")
            with Horizontal(classes="hmmer-row"):
                yield Label("E-value cutoff:", classes="hmmer-label")
                yield Input(value="1e-5", id="hmmer-evalue")
                yield Label("6-frame translate:", classes="hmmer-label")
                yield Switch(value=True, id="hmmer-translate")
            with Horizontal(classes="hmmer-row"):
                yield Button("Scan Selected", id="hmmer-run", variant="primary")
                yield Button("Scan Collection (PFAM)", id="hmmer-scan-all")
                yield Button("Confirm CDS (NCBI)", id="hmmer-confirm-cds")
                yield Static(id="hmmer-status")
        yield ProgressBar(total=100, show_eta=True, id="hmmer-progress")
        yield LoadingIndicator(id="hmmer-loading")
        with VerticalScroll(id="hmmer-results-area"):
            yield Static(id="hmmer-diagram")
            yield DataTable(id="hmmer-table", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#hmmer-table", DataTable).add_columns(
            "Family", "Accession", "E-value", "Score", "Bias",
            "HMM From", "HMM To", "Ali From", "Ali To", "Description",
        )
        if not hmmer_available():
            self.query_one("#hmmer-status", Static).update(
                "[yellow]pyhmmer not installed — run: pip install pyhmmer[/]"
            )
        # Auto-detect existing Pfam DB
        existing = pfam_db_exists()
        if existing:
            self.query_one("#hmmer-db-input", Input).value = existing
            self._load_descriptions_bg()

    def _load_descriptions_bg(self) -> None:
        clans_tsv = _PFAM_DEFAULT_DIR / "Pfam-A.clans.tsv"
        if clans_tsv.exists():
            self._do_load_descriptions(clans_tsv)

    @work(exclusive=False, thread=True, group="pfam-desc")
    def _do_load_descriptions(self, clans_tsv: Path) -> None:
        desc = load_pfam_descriptions(clans_tsv)
        def _apply() -> None:
            self._pfam_desc = desc
            _log.info("Loaded %d Pfam descriptions", len(desc))
        self.app.call_from_thread(_apply)

    def _show_progress(self, current: int, total: int) -> None:
        """Update progress bar from any thread via call_from_thread."""
        bar = self.query_one("#hmmer-progress", ProgressBar)
        bar.update(total=total, progress=current)

    @on(Button.Pressed, "#hmmer-download-pfam")
    def download_pfam_db(self) -> None:
        if not hmmer_available():
            self._set_status("[red]pyhmmer is required — run: pip install pyhmmer[/]")
            return
        self._run_pfam_download()

    @work(exclusive=True, thread=False)
    async def _run_pfam_download(self) -> None:
        progress = self.query_one("#hmmer-progress", ProgressBar)
        progress.add_class("running")
        btn = self.query_one("#hmmer-download-pfam", Button)
        btn.disabled = True

        def _progress(msg: str) -> None:
            self.app.call_from_thread(self._set_status, msg)
            self.app.call_from_thread(self.app.clear_notifications)
            self.app.call_from_thread(
                self.app.notify, msg, title="Pfam Download",
                severity="information", timeout=120,
            )

        try:
            hmm_path, descriptions = await download_pfam(progress_cb=_progress)
            self._pfam_desc = descriptions
            self.query_one("#hmmer-db-input", Input).value = hmm_path
            self.app.clear_notifications()
            self._set_status(
                f"[green]Pfam ready: {hmm_path} ({len(descriptions):,} families)[/]"
            )
            self.app.notify(
                f"Pfam database ready — {len(descriptions):,} families",
                title="Pfam Download", severity="information", timeout=5,
            )
        except Exception as exc:
            self.app.clear_notifications()
            self._set_status(f"[red]Download failed: {exc}[/]")
            self.app.notify(
                f"Download failed: {exc}", title="Pfam Download",
                severity="error", timeout=8,
            )
            _log.exception("Pfam download failed: %s", exc)
        finally:
            progress.remove_class("running")
            btn.disabled = False

    @on(Button.Pressed, "#hmmer-run")
    def run_scan(self) -> None:
        t = self.transcript
        if t is None:
            self._set_status("[yellow]Select a transcript first.[/]")
            return
        # Check cache first — instant result
        if t.id in self._scan_cache:
            hits = self._scan_cache[t.id]
            self._display_hits(t, hits)
            conf = self._confirm_cache.get(t.id)
            if conf:
                tag = "CONFIRMED" if conf.confirmed else "UNCONFIRMED"
                color = "green" if conf.confirmed else "yellow"
                self._set_status(f"[green]{len(hits)} domain hits (cached).[/] [{color}]CDS {tag}[/{color}]")
            else:
                self._set_status(f"[green]{len(hits)} domain hits (cached).[/]")
            return
        db = self.query_one("#hmmer-db-input", Input).value.strip()
        if not db:
            self._set_status("[yellow]Enter an HMM database path.[/]")
            return
        evalue_str = self.query_one("#hmmer-evalue", Input).value.strip()
        translate = self.query_one("#hmmer-translate", Switch).value
        try:
            evalue = float(evalue_str)
        except ValueError:
            self._set_status("[red]Invalid e-value.[/]")
            return
        self._run_hmmer_worker(t, db, evalue, translate)

    @work(exclusive=True, thread=False)
    async def _run_hmmer_worker(
        self, t: Transcript, db: str, evalue: float, translate: bool,
    ) -> None:
        progress = self.query_one("#hmmer-progress", ProgressBar)
        progress.update(total=100, progress=0)
        progress.add_class("running")
        self._set_status(f"Scanning {t.short_id} for Pfam domains…")

        def _on_progress(current: int, total: int) -> None:
            self.app.call_from_thread(self._show_progress, current, total)

        try:
            _hmm_cancel.clear()
            hits = await hmmscan(
                sequence=t.sequence, seq_id=t.id, hmm_db_path=db,
                evalue_threshold=evalue, translate=translate,
                progress_cb=_on_progress,
            )
            self._scan_cache[t.id] = hits
            self._display_hits(t, hits)
            self._set_status(f"[green]{len(hits)} Pfam domain hits found.[/]")
        except HMMCancelled:
            self._set_status("[dim]Scan cancelled.[/]")
        except Exception as exc:
            self._set_status(f"[red]Error: {exc}[/]")
        finally:
            progress.remove_class("running")

    @on(Button.Pressed, "#hmmer-confirm-cds")
    def confirm_cds(self) -> None:
        _log.info("Confirm CDS button pressed")
        t = self.transcript
        if t is None:
            self._set_status("[yellow]Select a transcript first.[/]")
            return
        if t.id in self._confirm_cache:
            hits = self._scan_cache.get(t.id, [])
            self._display_hits(t, hits)
            conf = self._confirm_cache[t.id]
            tag = "CONFIRMED" if conf.confirmed else "UNCONFIRMED"
            self._set_status(f"[green]CDS {tag} (cached).[/]")
            return
        self._set_status("Finding longest ORF…")
        best_orf = _find_longest_orf(t.sequence, t.id)
        if best_orf is None:
            self._set_status("[yellow]No ORF ≥ 30 aa found in this transcript.[/]")
            return
        _log.info("Launching NCBI blastp for %s (%d aa)", t.id, best_orf.aa_length)
        self._run_confirm_cds_worker(t, best_orf)

    @work(exclusive=True, thread=True, group="confirm-cds")
    def _run_confirm_cds_worker(self, t: Transcript, orf: ORFCoord) -> None:
        import time

        def _ui(fn, *a):
            self.app.call_from_thread(fn, *a)

        _log.info("confirm-cds worker started for %s (%d aa)", t.id, orf.aa_length)
        loading = self.query_one("#hmmer-loading", LoadingIndicator)
        _ui(loading.add_class, "running")
        _ui(self._set_status,
            f"NCBI blastp vs swissprot: {orf.aa_length} aa CDS… (may take 10–30 s)")

        try:
            from Bio.Blast import NCBIWWW, NCBIXML

            t0 = time.monotonic()
            _log.info("Submitting qblast to swissprot…")
            result_handle = NCBIWWW.qblast(
                "blastp", "swissprot", orf.sequence,
                hitlist_size=3,
                expect=1e-5,
            )
            elapsed = time.monotonic() - t0
            _log.info("qblast returned in %.1f s", elapsed)
            _ui(self._set_status, f"Parsing NCBI BLAST results… ({elapsed:.0f}s)")

            records = NCBIXML.parse(result_handle)
            record = next(records)
            result_handle.close()

            if not record.alignments:
                conf = BlastConfirmation(confirmed=False, query_length=orf.aa_length)
            else:
                best = record.alignments[0]
                hsp = best.hsps[0]
                query_len = record.query_length or orf.aa_length
                identity_pct = (hsp.identities / hsp.align_length) * 100 if hsp.align_length else 0
                coverage_pct = (hsp.align_length / query_len) * 100 if query_len else 0
                confirmed = (
                    hsp.expect <= 1e-5
                    and identity_pct >= 40.0
                    and coverage_pct >= 50.0
                )
                conf = BlastConfirmation(
                    confirmed=confirmed,
                    top_hit_name=(best.hit_def[:80] if best.hit_def else best.title[:80]),
                    top_hit_acc=best.accession or "",
                    identity_pct=identity_pct,
                    coverage_pct=coverage_pct,
                    evalue=hsp.expect,
                    query_length=query_len,
                    alignment_length=hsp.align_length,
                )

            self._confirm_cache[t.id] = conf

            def _final():
                hits = self._scan_cache.get(t.id, [])
                self._display_hits(t, hits)
                tag = "CONFIRMED" if conf.confirmed else "UNCONFIRMED"
                color = "green" if conf.confirmed else "yellow"
                msg = f"[{color}]CDS {tag}[/{color}]"
                if conf.top_hit_acc:
                    msg += f" — [dim]{conf.top_hit_acc} ({conf.identity_pct:.1f}% id, {conf.coverage_pct:.1f}% cov)[/dim]"
                self._set_status(msg)
            _ui(_final)

        except Exception as exc:
            _ui(self._set_status, f"[red]NCBI BLAST error: {exc}[/]")
            _log.exception("CDS confirmation failed: %s", exc)
        finally:
            _ui(loading.remove_class, "running")

    @on(Button.Pressed, "#hmmer-scan-all")
    def run_scan_all(self) -> None:
        if not self.app._transcripts:
            self._set_status("[yellow]No transcripts loaded.[/]")
            return
        db = self.query_one("#hmmer-db-input", Input).value.strip()
        if not db:
            self._set_status("[yellow]Enter an HMM database path.[/]")
            return
        evalue_str = self.query_one("#hmmer-evalue", Input).value.strip()
        translate = self.query_one("#hmmer-translate", Switch).value
        try:
            evalue = float(evalue_str)
        except ValueError:
            self._set_status("[red]Invalid e-value.[/]")
            return
        n = len(self.app._transcripts)
        est_minutes = max(1, n * 10 // 60)  # ~10s per transcript
        self.app.notify(
            f"This will scan {n:,} transcripts against the full Pfam database.\n"
            f"Estimated time: ~{est_minutes} min. Press Ctrl+C or quit to cancel.",
            title="Collection Scan",
            severity="warning",
            timeout=10,
        )
        self._run_scan_all_worker(self.app._transcripts, db, evalue, translate)

    @work(exclusive=True, thread=False)
    async def _run_scan_all_worker(
        self, transcripts: list[Transcript], db: str, evalue: float, translate: bool,
    ) -> None:
        import time

        progress = self.query_one("#hmmer-progress", ProgressBar)
        progress.update(total=100, progress=0)
        progress.add_class("running")
        self._set_status(f"Scanning {len(transcripts):,} transcripts…")
        t0 = time.monotonic()

        def _on_progress(current: int, total: int) -> None:
            elapsed = time.monotonic() - t0
            pct = current * 100 // total if total else 0
            if current > 0 and pct > 0:
                eta_s = elapsed / pct * (100 - pct)
                eta_str = f"{int(eta_s // 60)}m {int(eta_s % 60)}s"
            else:
                eta_str = "calculating…"
            self.app.call_from_thread(self._show_progress, current, total)
            self.app.call_from_thread(
                self._set_status,
                f"Scanning collection… {current:,}/{total:,} HMMs ({pct}%) "
                f"— elapsed {int(elapsed // 60)}m {int(elapsed % 60)}s, ETA {eta_str}",
            )

        try:
            _hmm_cancel.clear()
            mapping = await hmmsearch_all(
                transcripts=transcripts, hmm_db_path=db,
                evalue_threshold=evalue, translate=translate,
                progress_cb=_on_progress,
            )
            elapsed = time.monotonic() - t0
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            self.app._pfam_hits = mapping
            self._set_status(
                f"[green]Collection scan complete: {len(mapping)} transcripts "
                f"with hits ({elapsed_str}).[/]"
            )
            self.app._apply_filter()
        except HMMCancelled:
            self._set_status("[dim]Collection scan cancelled.[/]")
        except Exception as exc:
            self._set_status(f"[red]Scan failed: {exc}[/]")
            _log.exception("HmmerPanel._run_scan_all_worker failed: %s", exc)
        finally:
            progress.remove_class("running")

    def _set_status(self, msg: str) -> None:
        self.query_one("#hmmer-status", Static).update(msg)


# ══════════════════════════════════════════════════════════════════════════════
# Make BLAST DB modal
# ══════════════════════════════════════════════════════════════════════════════

class MakeDbModal(ModalScreen[Optional[str]]):
    DEFAULT_CSS = """
    MakeDbModal { align: center middle; }
    MakeDbModal #dialog {
        width: 70; height: 12;
        border: thick $accent 80%; background: $surface; padding: 1 2;
    }
    MakeDbModal #dialog-title { text-align: center; text-style: bold; margin-bottom: 1; }
    MakeDbModal #db-buttons { margin-top: 1; align: right middle; height: 3; }
    """

    def __init__(self, fasta_path: str) -> None:
        super().__init__()
        self._fasta_path = fasta_path

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Build BLAST Database", id="dialog-title")
            yield Static(f"Build from: [cyan]{self._fasta_path}[/]")
            yield Static(
                "This will run [bold]makeblastdb[/] and create index files next to the FASTA."
            )
            with Horizontal(id="db-buttons"):
                yield Button("Cancel", id="db-cancel")
                yield Button("Build", id="db-confirm", variant="primary")

    @on(Button.Pressed, "#db-confirm")
    def confirm(self) -> None:
        self.dismiss(self._fasta_path)

    @on(Button.Pressed, "#db-cancel")
    def cancel(self) -> None:
        self.dismiss(None)


# ══════════════════════════════════════════════════════════════════════════════
# Main application
# ══════════════════════════════════════════════════════════════════════════════

_MAX_TABLE_ROWS = 2_000
_FILTER_DEBOUNCE = 0.25


class ScriptoScopeApp(App):
    TITLE = f"ScriptoScope v{__version__}"
    SUB_TITLE = "Transcriptome Browser"

    CSS = """
    #main-layout { height: 1fr; }
    #sidebar {
        width: 32; min-width: 20; max-width: 60;
        border-right: solid $primary 50%; height: 100%;
    }
    #sidebar-header {
        height: 3; background: $surface;
        padding: 0 1; border-bottom: solid $primary 30%;
    }
    #transcript-count { color: $text-muted; width: 1fr; content-align: right middle; }
    #sidebar-filters {
        height: auto; padding: 1;
        background: $surface; border-bottom: solid $primary 20%;
    }
    #sidebar-filters Input { margin-bottom: 1; }
    #transcript-table { height: 1fr; }
    #content-area { width: 1fr; height: 100%; }
    #app-status {
        height: 1; background: $surface;
        padding: 0 1; color: $text-muted;
        border-top: solid $primary 20%;
    }
    """

    BINDINGS = [
        Binding("ctrl+o", "open_file", "Open FASTA"),
        Binding("ctrl+b", "focus_blast", "BLAST"),
        Binding("ctrl+p", "focus_pfam", "Pfam Scan"),
        Binding("ctrl+f", "focus_filter", "Filter"),
        Binding("ctrl+d", "build_blast_db", "Build BLAST DB"),
        Binding("q", "quit", "Quit"),
    ]

    def action_quit(self) -> None:
        _hmm_cancel.set()
        _restore_terminal()
        os._exit(0)

    def __init__(self, startup_fasta: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._startup_fasta = startup_fasta
        self._transcripts: list[Transcript] = []
        self._filtered: list[Transcript] = []
        self._by_id: dict[str, Transcript] = {}
        self._fasta_path: str = ""
        self._blast_db: str = ""
        self._pfam_hits: dict[str, set[str]] = {}
        self._filter_timer: Timer | None = None
        self._navigating_to: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            with Vertical(id="sidebar"):
                with Horizontal(id="sidebar-header"):
                    yield Label("[bold]Transcripts[/]")
                    yield Static("0 loaded", id="transcript-count")
                with Vertical(id="sidebar-filters"):
                    yield Input(placeholder="Filter by ID…", id="filter-input")
                yield DataTable(
                    id="transcript-table",
                    zebra_stripes=True,
                    cursor_type="row",
                    show_header=True,
                )
            with TabbedContent(id="content-area"):
                with TabPane("Sequence", id="tab-sequence"):
                    yield SequenceViewer(id="seq-viewer")
                with TabPane("BLAST", id="tab-blast"):
                    yield BlastPanel(id="blast-panel")
                with TabPane("Pfam / HMM", id="tab-hmmer"):
                    yield HmmerPanel(id="hmmer-panel")
                with TabPane("Statistics", id="tab-stats"):
                    yield StatsPanel(id="stats-panel")
        yield Static("No file loaded. Press Ctrl+O to open a FASTA file.", id="app-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#transcript-table", DataTable)
        table.add_columns("ID", "Length", "GC%")
        table.focus()
        if self._startup_fasta:
            self._load_fasta(self._startup_fasta)
        # Pre-warm HMM cache in background so first scan is fast
        existing = pfam_db_exists()
        if existing and hmmer_available():
            self._prewarm_hmm_cache(existing)

    @work(exclusive=False, thread=True, group="hmm-prewarm")
    def _prewarm_hmm_cache(self, db_path: str) -> None:
        _log.info("Pre-warming HMM cache for %s", db_path)
        _hmm_cache.get(db_path)
        _log.info("HMM cache pre-warmed")

    # ── File loading ──────────────────────────────────────────────────────────

    def action_open_file(self) -> None:
        start = self._fasta_path or str(Path.home())
        self.push_screen(FileBrowserModal(start_path=start), self._on_file_selected)

    def _on_file_selected(self, path: str | None) -> None:
        if path:
            self._load_fasta(path)

    @work(exclusive=True, thread=True)
    def _load_fasta(self, path: str) -> None:
        self.call_from_thread(self._set_status, f"Loading {path}…")
        self.call_from_thread(
            self.notify, f"Loading {Path(path).name}…",
            title="Transcriptome", severity="information", timeout=60,
        )
        try:
            transcripts = load_all(path)
        except Exception as exc:
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(self._set_status, f"[red]Error loading file: {exc}[/]")
            return

        by_id = {t.id: t for t in transcripts}

        def _apply() -> None:
            self.clear_notifications()
            self._transcripts = transcripts
            self._filtered = transcripts
            self._by_id = by_id
            self._fasta_path = path
            self._populate_table(transcripts)
            self._set_status(
                f"[green]{len(transcripts):,} transcripts loaded from {path}[/]"
            )
            self.notify(
                f"{len(transcripts):,} transcripts loaded",
                title="Transcriptome", severity="information", timeout=3,
            )
            # Compute stats in background (triggers lazy GC on all transcripts)
            self._compute_stats_bg(transcripts, path)
            if blast_available():
                self.action_build_blast_db(interactive=False)

        self.call_from_thread(_apply)

    @work(exclusive=False, thread=True, group="stats")
    def _compute_stats_bg(self, transcripts: list[Transcript], path: str) -> None:
        stats = _compute_stats(transcripts)
        def _apply_stats() -> None:
            self.query_one("#stats-panel", StatsPanel).render_stats(stats, path)
        self.call_from_thread(_apply_stats)

    def _populate_table(self, transcripts: list[Transcript], *, auto_select: bool = True) -> None:
        _log.debug("_populate_table: %d transcripts", len(transcripts))
        table = self.query_one("#transcript-table", DataTable)
        visible = transcripts[:_MAX_TABLE_ROWS]
        table.clear()
        for t in visible:
            table.add_row(t.short_id, f"{t.length:,}", f"{t.gc_content:.1f}", key=t.id)
        total = len(transcripts)
        shown = len(visible)
        count_text = f"{shown:,} of {total:,} shown" if shown < total else f"{total:,} shown"
        self.query_one("#transcript-count", Static).update(count_text)
        if visible and auto_select:
            table.focus()
            table.move_cursor(row=0)
            _log.debug("_populate_table: showing first transcript %r", visible[0].id)
            self._show_transcript(visible[0])

    # ── Filtering ─────────────────────────────────────────────────────────────

    @on(Input.Changed, "#filter-input")
    def filter_transcripts(self, event: Input.Changed) -> None:
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(
            _FILTER_DEBOUNCE, self._apply_filter
        )

    def _apply_filter(self) -> None:
        # Skip if we're navigating to a BLAST hit — the table was already
        # repopulated by _focus_transcript_from_blast.
        if self._navigating_to is not None:
            return
        query = self.query_one("#filter-input", Input).value.strip().lower()
        results = self._transcripts
        if query:
            results = [t for t in results if query in t.id.lower()]
        self._filtered = results
        self._populate_table(self._filtered)

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    # ── Transcript selection ──────────────────────────────────────────────────

    def _show_transcript(self, t: Transcript) -> None:
        try:
            self.query_one("#seq-viewer", SequenceViewer).transcript = t
            self.query_one("#stats-panel", StatsPanel).transcript = t
            self.query_one("#blast-panel", BlastPanel).transcript = t
            self.query_one("#hmmer-panel", HmmerPanel).transcript = t
        except Exception as exc:
            _log.exception("_show_transcript failed: %s", exc)
            self._set_status(f"[red]Error displaying transcript: {exc}[/]")

    def _select_by_row_key(self, row_key) -> None:
        if row_key is None:
            return
        t = self._by_id.get(str(row_key.value))
        _log.debug("_select_by_row_key: key=%r -> found=%s", row_key.value, t is not None)
        if t is not None:
            self._show_transcript(t)

    # Use Textual's method-name convention (on_<namespace>_<message>) instead of
    # @on with CSS selector — the CSS selector matching is unreliable in 8.1.1.
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "transcript-table":
            # If we're navigating to a specific transcript (e.g. from a BLAST
            # hit click), ignore highlight events for any other row.
            if self._navigating_to is not None:
                if event.row_key and event.row_key.value == self._navigating_to:
                    self._navigating_to = None
                    self._select_by_row_key(event.row_key)
                return
            self._select_by_row_key(event.row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "transcript-table":
            self._select_by_row_key(event.row_key)
        elif event.data_table.id == "blast-table":
            self._focus_transcript_from_blast(event.row_key)

    def _focus_transcript_from_blast(self, row_key) -> None:
        if row_key is None:
            return
        subject_id = str(row_key.value)
        t = self._by_id.get(subject_id)
        if t is None:
            self._set_status(f"[yellow]'{subject_id}' not found in loaded transcriptome[/]")
            return
        # Set the navigation guard — this makes on_data_table_row_highlighted
        # ignore all highlight events until the target row is reached.
        self._navigating_to = subject_id
        # Cancel any pending filter timer so it doesn't repopulate later
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None
        # Clear filter so the transcript is visible in the full table
        filter_input = self.query_one("#filter-input", Input)
        needs_rebuild = filter_input.value.strip() != "" or self._filtered is not self._transcripts
        filter_input.value = ""
        if needs_rebuild:
            self._filtered = self._transcripts
            self._populate_table(self._filtered, auto_select=False)
        # Find the row for this transcript and move cursor to it
        table = self.query_one("#transcript-table", DataTable)
        try:
            row_idx = next(
                i for i, key in enumerate(table.rows) if key.value == subject_id
            )
            table.move_cursor(row=row_idx, animate=False, scroll=True)
            table.focus()
        except StopIteration:
            pass
        self._show_transcript(t)
        # Switch to sequence tab to show the hit
        self.query_one("#content-area", TabbedContent).active = "tab-sequence"

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "tab-blast" and self._blast_db:
            self.query_one("#blast-panel", BlastPanel).set_transcriptome_db(self._blast_db)
        elif event.pane.id == "tab-hmmer" and self._fasta_path:
            # Check for .hmm or .h3m next to FASTA
            base = Path(self._fasta_path).with_suffix("")
            for ext in [".hmm", ".h3m"]:
                hmm_path = base.with_suffix(ext)
                if hmm_path.exists():
                    self.query_one("#hmmer-panel", HmmerPanel).set_db_path(str(hmm_path))
                    break

    # ── Tab shortcuts ─────────────────────────────────────────────────────────

    def action_focus_blast(self) -> None:
        self.query_one("#content-area", TabbedContent).active = "tab-blast"

    def action_focus_pfam(self) -> None:
        self.query_one("#content-area", TabbedContent).active = "tab-hmmer"

    # ── BLAST DB builder ──────────────────────────────────────────────────────

    def action_build_blast_db(self, interactive: bool = True) -> None:
        if not self._fasta_path:
            self._set_status("[yellow]Open a FASTA file first.[/]")
            return
        if not blast_available():
            self._set_status("[red]BLAST+ not installed. Cannot build database.[/]")
            return
        if interactive:
            self.push_screen(MakeDbModal(self._fasta_path), self._on_build_db)
        else:
            self._build_db_worker(self._fasta_path)

    def _on_build_db(self, fasta_path: str | None) -> None:
        if fasta_path:
            self._build_db_worker(fasta_path)

    @work(exclusive=False, thread=False)
    async def _build_db_worker(self, fasta_path: str) -> None:
        self._set_status("Building BLAST database…")
        self.notify(
            "Building BLAST database…", title="BLAST",
            severity="information", timeout=120,
        )
        try:
            db_path = await make_blast_db(fasta_path)
            self._blast_db = db_path
            self.query_one("#blast-panel", BlastPanel).set_transcriptome_db(db_path)
            self.clear_notifications()
            self._set_status(f"[green]BLAST DB ready: {db_path}[/]")
            self.notify(
                "BLAST database ready", title="BLAST",
                severity="information", timeout=3,
            )
        except RuntimeError as exc:
            self.clear_notifications()
            self._set_status(f"[red]makeblastdb error: {exc}[/]")
            self.notify(
                f"makeblastdb error: {exc}", title="BLAST",
                severity="error", timeout=5,
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        self.query_one("#app-status", Static).update(msg)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def _restore_terminal() -> None:
    """Reset terminal to a sane state before force-exiting."""
    import sys
    # Silence other threads immediately by redirecting Python-level IO
    devnull = open(os.devnull, "w")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    sys.stdout = devnull
    sys.stderr = devnull

    # Write reset sequences directly to the terminal fd
    reset = (
        "\x1b[?1000l"   # disable mouse click tracking
        "\x1b[?1002l"   # disable mouse drag tracking
        "\x1b[?1003l"   # disable mouse move tracking
        "\x1b[?1006l"   # disable SGR mouse mode
        "\x1b[?1015l"   # disable urxvt mouse mode
        "\x1b[?2004l"   # disable bracketed paste
        "\x1b[?1l"      # reset cursor keys to normal
        "\x1b[?25h"     # show cursor
        "\x1b[?1049l"   # leave alternate screen
    )
    try:
        fd = os.open("/dev/tty", os.O_WRONLY)
        os.write(fd, reset.encode())
        os.close(fd)
    except Exception:
        pass
    os.system("stty sane 2>/dev/null")


def main() -> None:
    signal.signal(signal.SIGINT, lambda *_: (_restore_terminal(), os._exit(0)))

    parser = argparse.ArgumentParser(
        prog="scriptoscope",
        description=f"ScriptoScope v{__version__} — TUI Transcriptome Browser",
    )
    parser.add_argument("fasta", nargs="?", help="FASTA file to open on startup")
    args = parser.parse_args()
    ScriptoScopeApp(startup_fasta=args.fasta or "").run()
    os._exit(0)


if __name__ == "__main__":
    main()
