"""
ScriptoScope — TUI Transcriptome Browser

Changelog:
  0.1.0 — initial release: FASTA loading, sequence viewer, BLAST, HMMER, statistics
  0.2.0 — file-browser dialog, scrollable widgets, thread-safe UI updates,
           run-length encoded sequence coloring, single-pass stats
  0.3.0 — consolidated into single-file script, fixed transcript selection,
           moved _compute_stats off the main thread
  0.4.0 — switched to method-name event handlers, fixed threaded render races,
           added error surfacing + debug log at /tmp/scriptoscope.log
  0.5.0 — export, filtering, sorting, bookmarks, help, ORF stats,
           HMM scan performance + UI responsiveness
  0.6.0 — gzip FASTA support, atomic project saves, duplicate-ID dedup,
           in-dialog save feedback, project version validation,
           NCBI RID cleanup on cancel, cache correctness fixes,
           translate() partial-codon warnings silenced
"""
from __future__ import annotations

__version__ = "0.6.0"

# ── stdlib ────────────────────────────────────────────────────────────────────
import argparse
import asyncio
import csv
import gzip
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import Counter, OrderedDict
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path
from typing import Callable, Generator, Optional

# ── Logging ───────────────────────────────────────────────────────────────────
# Every session writes to a rotating log file. Default is /tmp/scriptoscope.log;
# override with the SCRIPTOSCOPE_LOG env var. Each session gets a unique 8-char
# session ID prefix so multi-run logs can be cleanly separated with grep.
#
# If a user reports a bug, ask them to share the last run's log lines (find the
# newest SESSION ID at the top of the file and copy from there). The startup
# banner below captures version, platform, and dependency info so reproducing
# the environment is trivial.
import uuid as _uuid
from logging.handlers import RotatingFileHandler

_LOG_PATH = os.environ.get("SCRIPTOSCOPE_LOG") or "/tmp/scriptoscope.log"
_SESSION_ID = _uuid.uuid4().hex[:8]


class _SessionFilter(logging.Filter):
    """Inject the per-session ID into every log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.session = _SESSION_ID
        return True


def _configure_logging() -> logging.Logger:
    root = logging.getLogger("scriptoscope")
    root.setLevel(logging.DEBUG)
    # Idempotent: if Python re-imports the module (e.g. in tests), don't
    # stack duplicate handlers on top of each other.
    for h in list(root.handlers):
        root.removeHandler(h)
    try:
        Path(_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            _LOG_PATH,
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=3,              # keep 3 rotated copies
            encoding="utf-8",
        )
    except OSError:
        # Fallback: if we can't open the log file, write to stderr so at
        # least errors are visible somewhere.
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s [%(session)s] %(levelname)-5s "
            "%(name)s.%(funcName)s:%(lineno)d %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.addFilter(_SessionFilter())
    root.addHandler(handler)
    root.propagate = False
    return root


_log = _configure_logging()


def _log_startup_banner() -> None:
    """Log a clear, self-contained session header. Every bug report starts
    here — tell me the session ID and I can tell you exactly what was running."""
    import platform, sys as _sys
    banner_lines = [
        "=" * 70,
        f"ScriptoScope session {_SESSION_ID} starting",
        f"  version         : {__version__}",
        f"  python          : {_sys.version.split()[0]} ({platform.python_implementation()})",
        f"  platform        : {platform.platform()}",
        f"  cwd             : {os.getcwd()}",
        f"  argv            : {_sys.argv}",
        f"  log file        : {_LOG_PATH}",
        f"  pid             : {os.getpid()}",
    ]
    # Best-effort dependency version capture. importlib.metadata works for
    # any installed distribution; module.__version__ is unreliable (e.g.
    # rich doesn't expose it directly).
    try:
        from importlib.metadata import version as _pkg_version, PackageNotFoundError
    except ImportError:
        _pkg_version = None  # type: ignore

    def _safe_version(dist_name: str, import_name: str) -> str:
        if _pkg_version is not None:
            try:
                return _pkg_version(dist_name)
            except Exception:
                pass
        try:
            mod = __import__(import_name)
            return getattr(mod, "__version__", "unknown")
        except ImportError:
            return "NOT INSTALLED"

    for pkg_name, dist_name, import_name in [
        ("textual", "textual", "textual"),
        ("rich", "rich", "rich"),
        ("biopython", "biopython", "Bio"),
        ("pyhmmer", "pyhmmer", "pyhmmer"),
    ]:
        banner_lines.append(f"  {pkg_name:<15} : {_safe_version(dist_name, import_name)}")
    banner_lines.append("=" * 70)
    for line in banner_lines:
        _log.info(line)


def _install_exception_hooks() -> None:
    """Capture unhandled exceptions from the main thread AND worker threads.
    Without this, a crash in a background worker is silently lost to the TUI."""
    import sys as _sys

    prev_excepthook = _sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        _log.critical(
            "UNCAUGHT EXCEPTION (main thread)",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        prev_excepthook(exc_type, exc_value, exc_tb)

    _sys.excepthook = _hook

    # threading.excepthook exists in Python 3.8+
    prev_thread_hook = getattr(threading, "excepthook", None)

    def _thread_hook(args):
        _log.critical(
            "UNCAUGHT EXCEPTION in thread %s",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        if prev_thread_hook is not None:
            prev_thread_hook(args)

    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


_install_exception_hooks()

# ── third-party ───────────────────────────────────────────────────────────────
from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.timer import Timer
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

class FastaFormatError(ValueError):
    """Raised when a file does not look like a FASTA."""


def _open_fasta(path: Path):
    """Open a FASTA file, transparently handling gzip-compressed files.

    Detects gzip by magic bytes rather than extension so mislabeled files
    still work.
    """
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def _parse_fasta(path: str | Path) -> Generator[Transcript, None, None]:
    path = Path(path)
    seq_id = None
    description = ""
    seq_parts: list[str] = []
    saw_header = False
    saw_content = False

    with _open_fasta(path) as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.strip():
                saw_content = True
            if line.startswith(">"):
                saw_header = True
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

    if saw_content and not saw_header:
        raise FastaFormatError(
            f"{path.name} does not look like a FASTA file (no '>' header found)"
        )

    if seq_id is not None:
        yield Transcript(id=seq_id, description=description,
                         sequence="".join(seq_parts))


def load_all(path: str | Path) -> list[Transcript]:
    """Load all transcripts from a FASTA file, deduplicating IDs.

    Duplicate IDs are renamed with a numeric suffix (e.g. "foo", "foo__2",
    "foo__3") and the total number of duplicates is logged.
    """
    transcripts: list[Transcript] = []
    seen_ids: dict[str, int] = {}
    duplicate_count = 0
    for t in _parse_fasta(path):
        prev = seen_ids.get(t.id)
        if prev is None:
            seen_ids[t.id] = 1
        else:
            duplicate_count += 1
            prev += 1
            seen_ids[t.id] = prev
            new_id = f"{t.id}__{prev}"
            # Rebuild Transcript with a unique ID so _by_id is lossless
            t = Transcript(id=new_id, description=t.description, sequence=t.sequence)
        transcripts.append(t)
    if duplicate_count:
        _log.warning(
            "Loaded %s: %d duplicate transcript ID(s) renamed with __N suffix",
            path, duplicate_count,
        )
    return transcripts


# ══════════════════════════════════════════════════════════════════════════════
# Project save / load (JSON)
# ══════════════════════════════════════════════════════════════════════════════

_PROJECT_VERSION = 1


def save_project(
    path: str | Path,
    transcripts: list[Transcript],
    fasta_path: str | None,
    scan_cache: dict[str, list] | None = None,
    confirm_cache: dict | None = None,
    pfam_hits: dict[str, set[str]] | None = None,
) -> None:
    """Save transcriptome + analysis results to a JSON project file."""
    data: dict = {
        "version": _PROJECT_VERSION,
        "fasta_path": fasta_path or "",
        "transcripts": [
            {"id": t.id, "description": t.description, "sequence": t.sequence}
            for t in transcripts
        ],
    }
    if scan_cache:
        data["scan_cache"] = {
            tid: [asdict(h) for h in hits]
            for tid, hits in scan_cache.items()
        }
    if confirm_cache:
        data["confirm_cache"] = {
            tid: asdict(conf)
            for tid, conf in confirm_cache.items()
        }
    if pfam_hits:
        data["pfam_hits"] = {
            tid: sorted(families)
            for tid, families in pfam_hits.items()
        }
    # Atomic write: serialize to a sibling temp file, then rename.
    # Avoids leaving a half-written project file if the process dies mid-write.
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ProjectFormatError(ValueError):
    """Raised when a project file is malformed or from an unsupported version."""


def _coerce_dataclass(cls, raw: dict):
    """Build a dataclass from a dict, ignoring unknown keys and filling
    defaults for missing ones. Tolerates forward/backward-compat drift."""
    import dataclasses
    fields = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in raw.items() if k in fields}
    return cls(**kwargs)


def load_project(path: str | Path) -> dict:
    """Load a JSON project file and return the raw dict.

    Returns dict with keys: transcripts, fasta_path, scan_cache,
    confirm_cache, pfam_hits.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ProjectFormatError(f"Invalid JSON in {Path(path).name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ProjectFormatError(
            f"{Path(path).name} is not a ScriptoScope project (expected JSON object)"
        )

    version = raw.get("version")
    if version is None:
        raise ProjectFormatError(
            f"{Path(path).name} is missing a 'version' field — not a project file?"
        )
    if not isinstance(version, int) or version > _PROJECT_VERSION:
        raise ProjectFormatError(
            f"{Path(path).name} uses project version {version}; "
            f"this build understands up to version {_PROJECT_VERSION}"
        )

    raw_transcripts = raw.get("transcripts", [])
    if not isinstance(raw_transcripts, list):
        raise ProjectFormatError("'transcripts' must be a list")

    result: dict = {"fasta_path": raw.get("fasta_path", "") or ""}
    transcripts: list[Transcript] = []
    for i, t in enumerate(raw_transcripts):
        if not isinstance(t, dict) or "id" not in t or "sequence" not in t:
            raise ProjectFormatError(f"Transcript #{i} is missing id or sequence")
        transcripts.append(Transcript(
            id=str(t["id"]),
            description=str(t.get("description", "")),
            sequence=str(t["sequence"]),
        ))
    result["transcripts"] = transcripts

    scan_cache: dict[str, list] = {}
    for tid, hit_list in (raw.get("scan_cache") or {}).items():
        if not isinstance(hit_list, list):
            continue
        parsed: list[HmmerHit] = []
        for h in hit_list:
            if isinstance(h, dict):
                try:
                    parsed.append(_coerce_dataclass(HmmerHit, h))
                except (TypeError, ValueError) as exc:
                    _log.warning("Skipping malformed HmmerHit for %s: %s", tid, exc)
        scan_cache[str(tid)] = parsed
    result["scan_cache"] = scan_cache

    confirm_cache: dict = {}
    for tid, conf in (raw.get("confirm_cache") or {}).items():
        if isinstance(conf, dict):
            try:
                confirm_cache[str(tid)] = _coerce_dataclass(BlastConfirmation, conf)
            except (TypeError, ValueError) as exc:
                _log.warning("Skipping malformed BlastConfirmation for %s: %s", tid, exc)
    result["confirm_cache"] = confirm_cache

    pfam_hits: dict[str, set[str]] = {}
    for tid, families in (raw.get("pfam_hits") or {}).items():
        if isinstance(families, (list, tuple, set)):
            pfam_hits[str(tid)] = {str(f) for f in families}
    result["pfam_hits"] = pfam_hits

    return result


# ══════════════════════════════════════════════════════════════════════════════
# GenBank transcriptome search
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class GenBankResult:
    """A single GenBank search result."""
    accession: str
    title: str
    organism: str
    seq_count: int = 0
    update_date: str = ""


def _entrez_available() -> bool:
    try:
        from Bio import Entrez  # noqa: F401
        return True
    except ImportError:
        return False


def genbank_search_transcriptomes(query: str, max_results: int = 10) -> list[GenBankResult]:
    """Search NCBI Nucleotide for TSA transcriptome assemblies by organism name.

    Uses a cascading search strategy: TSA master records first, then TSA keyword,
    then transcriptome in title, then free text. Returns up to max_results entries.
    """
    from Bio import Entrez
    Entrez.email = "scriptoscope@example.com"

    # Cascading search strategies — TSA records only
    strategies = [
        f"{query}[Organism] AND tsa-master[prop]",
        f"{query}[Organism] AND TSA[Keyword]",
        f"{query} AND tsa-master[prop]",
        f"{query} AND TSA[Keyword]",
    ]

    id_list: list[str] = []
    for search_term in strategies:
        handle = Entrez.esearch(db="nuccore", term=search_term, retmax=max_results,
                                sort="relevance", usehistory="n")
        search_results = Entrez.read(handle)
        handle.close()
        if "ErrorList" in search_results:
            _log.warning("NCBI search error: %s", search_results["ErrorList"])
        id_list = search_results.get("IdList", [])
        if id_list:
            break

    if not id_list:
        return []

    # Fetch summaries
    handle = Entrez.esummary(db="nuccore", id=",".join(id_list), retmax=max_results)
    summaries = Entrez.read(handle)
    handle.close()
    if isinstance(summaries, dict) and "ERROR" in summaries:
        raise RuntimeError(f"NCBI summary fetch failed: {summaries['ERROR']}")

    results: list[GenBankResult] = []
    for doc in summaries:
        acc = doc.get("AccessionVersion", doc.get("Caption", ""))
        title = doc.get("Title", "")
        organism = doc.get("Organism", "")
        length = int(doc.get("Length", 0))
        update = doc.get("UpdateDate", "")
        results.append(GenBankResult(
            accession=acc, title=title, organism=organism,
            seq_count=length, update_date=update,
        ))
    return results


def _parse_tsa_range(accession: str) -> tuple[str, int, int, int] | None:
    """Parse the TSA contig range from a GenBank master record.

    Returns (prefix, first_num, last_num, num_width) or None if not a TSA master.
    """
    import re
    from Bio import Entrez
    Entrez.email = "scriptoscope@example.com"

    handle = Entrez.efetch(db="nuccore", id=accession, rettype="gb", retmode="text")
    data = handle.read()
    handle.close()

    for line in data.split("\n"):
        line = line.strip()
        if line.startswith("TSA") and "-" in line:
            # e.g. "TSA         GLAY01000001-GLAY01100949"
            parts = line.split()
            if len(parts) >= 2 and "-" in parts[-1]:
                range_str = parts[-1]
                first, last = range_str.split("-")
                m1 = re.match(r"([A-Z]+)(\d+)", first)
                m2 = re.match(r"([A-Z]+)(\d+)", last)
                if m1 and m2:
                    prefix = m1.group(1)
                    num_width = len(m1.group(2))
                    return prefix, int(m1.group(2)), int(m2.group(2)), num_width
    return None


async def genbank_download_fasta(
    accession: str,
    output_dir: str | Path,
    progress_cb: Callable[[str], None] | None = None,
) -> str:
    """Download a GenBank TSA accession as multi-contig FASTA.

    For TSA master records, parses the contig range and fetches in batches.
    Returns the path to the downloaded FASTA file.
    """
    from Bio import Entrez
    Entrez.email = "scriptoscope@example.com"

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{accession}.fasta"

    def _download() -> str:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        if progress_cb:
            progress_cb("Reading TSA master record…")

        tsa_range = _parse_tsa_range(accession)

        if tsa_range is None:
            # Not a TSA master — try direct FASTA download
            if progress_cb:
                progress_cb(f"Downloading {accession}…")
            handle = Entrez.efetch(db="nuccore", id=accession,
                                   rettype="fasta", retmode="text")
            data = handle.read()
            handle.close()
            if data.strip():
                out_path.write_text(data, encoding="utf-8")
                return str(out_path)
            raise RuntimeError(f"Empty FASTA for {accession}")

        prefix, first_num, last_num, num_width = tsa_range
        total = last_num - first_num + 1

        if progress_cb:
            progress_cb(f"Found {total:,} contigs. Downloading…")

        # Build batches of accession IDs
        batch_size = 500
        max_retries = 3
        n_workers = 3
        batches: list[list[str]] = []
        for start in range(first_num, last_num + 1, batch_size):
            end = min(start + batch_size - 1, last_num)
            batches.append([
                f"{prefix}{i:0{num_width}d}"
                for i in range(start, end + 1)
            ])

        fetched = [0]
        lock = threading.Lock()

        def _fetch_batch(batch_idx: int, accs: list[str]) -> tuple[int, str]:
            for attempt in range(max_retries):
                try:
                    h = Entrez.efetch(
                        db="nuccore", id=",".join(accs),
                        rettype="fasta", retmode="text",
                    )
                    data = h.read()
                    h.close()
                    with lock:
                        fetched[0] += len(accs)
                        if progress_cb:
                            progress_cb(
                                f"Downloaded {fetched[0]:,} of {total:,} contigs…"
                            )
                    return batch_idx, data
                except Exception:
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(2)
            return batch_idx, ""  # unreachable

        if progress_cb:
            progress_cb(
                f"Downloading {total:,} contigs ({len(batches)} batches, "
                f"{n_workers} parallel)…"
            )

        # Fetch concurrently, collect results in order
        results: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_fetch_batch, i, accs): i
                for i, accs in enumerate(batches)
            }
            for fut in as_completed(futures):
                idx, data = fut.result()
                results[idx] = data

        # Write in order
        with open(out_path, "w", encoding="utf-8") as fh:
            for i in range(len(batches)):
                fh.write(results[i])

        return str(out_path)

    return await asyncio.get_running_loop().run_in_executor(None, _download)


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


_ncbi_blast_cancel = threading.Event()


class NCBIBlastCancelled(Exception):
    """Raised when user cancels an NCBI BLAST search."""


def ncbi_blastp(
    query_seq: str,
    evalue: float = 1e-5,
    max_hits: int = 10,
    database: str = "nr",
    progress_cb: Callable[[str], None] | None = None,
) -> list[BlastHit]:
    """Run NCBI remote BlastP using the BLAST URL API directly.

    Submits the job, then polls with short intervals for faster response.
    """
    import socket
    import xml.etree.ElementTree as ET
    from urllib.parse import urlencode

    _BLAST_URL = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"

    def _ncbi_urlopen(req, timeout=30):
        """Open URL with user-friendly error messages for network failures."""
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise RuntimeError("Connection to NCBI timed out — check your internet connection") from exc
            raise RuntimeError(f"Cannot reach NCBI: {exc.reason}") from exc
        except socket.timeout:
            raise RuntimeError("Connection to NCBI timed out — check your internet connection")

    # ── Submit ────────────────────────────────────────────────────────────
    if progress_cb:
        progress_cb("Submitting BlastP to NCBI…")

    params = urlencode({
        "CMD": "Put",
        "PROGRAM": "blastp",
        "DATABASE": database,
        "QUERY": query_seq,
        "EXPECT": str(evalue),
        "HITLIST_SIZE": str(max_hits),
        "FORMAT_TYPE": "XML",
    }).encode()

    req = urllib.request.Request(_BLAST_URL, data=params)
    with _ncbi_urlopen(req, timeout=30) as resp:
        put_text = resp.read().decode()

    # Extract RID
    rid = ""
    for line in put_text.splitlines():
        if line.strip().startswith("RID = "):
            rid = line.strip().split("=", 1)[1].strip()
            break
    if not rid:
        raise RuntimeError("NCBI BLAST did not return a RID")

    def _delete_rid() -> None:
        """Tell NCBI to release the RID (best-effort; ignore failures)."""
        try:
            del_params = urlencode({"CMD": "Delete", "RID": rid}).encode()
            del_req = urllib.request.Request(_BLAST_URL, data=del_params)
            with urllib.request.urlopen(del_req, timeout=10):
                pass
        except Exception:
            pass

    # ── Poll for results ──────────────────────────────────────────────────
    poll_interval = 5  # seconds — much faster than qblast's 60s default
    elapsed = 0
    max_wait = 300  # 5 min timeout

    try:
        while elapsed < max_wait:
            # Wait up to poll_interval seconds, returning immediately on cancel.
            if _ncbi_blast_cancel.wait(timeout=poll_interval):
                raise NCBIBlastCancelled()
            elapsed += poll_interval
            if progress_cb:
                progress_cb(f"Waiting for NCBI BlastP results… ({elapsed}s)")

            check_params = urlencode({
                "CMD": "Get",
                "FORMAT_OBJECT": "SearchInfo",
                "RID": rid,
            }).encode()
            req = urllib.request.Request(_BLAST_URL, data=check_params)
            with _ncbi_urlopen(req, timeout=30) as resp:
                status_text = resp.read().decode()

            if "Status=WAITING" in status_text:
                continue
            if "Status=FAILED" in status_text:
                raise RuntimeError("NCBI BLAST job failed")
            if "Status=READY" in status_text:
                break
        else:
            raise RuntimeError(f"NCBI BLAST timed out after {max_wait}s")
    except NCBIBlastCancelled:
        # Release the server-side job so it doesn't keep running unattended.
        _delete_rid()
        raise

    # ── Fetch results ─────────────────────────────────────────────────────
    if progress_cb:
        progress_cb("Downloading NCBI BlastP results…")

    get_params = urlencode({
        "CMD": "Get",
        "FORMAT_TYPE": "XML",
        "RID": rid,
    }).encode()
    req = urllib.request.Request(_BLAST_URL, data=get_params)
    with _ncbi_urlopen(req, timeout=60) as resp:
        xml_data = resp.read().decode()

    # ── Parse XML ─────────────────────────────────────────────────────────
    if progress_cb:
        progress_cb("Parsing NCBI BlastP results…")

    hits: list[BlastHit] = []
    root = ET.fromstring(xml_data)
    for iteration in root.findall(".//Iteration"):
        query_id = iteration.findtext("Iteration_query-def", "query")
        for hit in iteration.findall(".//Hit"):
            hit_def = hit.findtext("Hit_def", "")
            hit_acc = hit.findtext("Hit_accession", "")
            subject_label = hit_acc
            if hit_def:
                subject_label = f"{hit_acc} {hit_def[:80]}"
            for hsp in hit.findall(".//Hsp"):
                try:
                    identity = int(hsp.findtext("Hsp_identity", "0"))
                    align_len = int(hsp.findtext("Hsp_align-len", "1"))
                    pct_id = (identity / align_len * 100) if align_len else 0.0
                    hits.append(BlastHit(
                        query_id=query_id,
                        subject_id=subject_label,
                        pct_identity=pct_id,
                        alignment_length=align_len,
                        mismatches=int(hsp.findtext("Hsp_gaps", "0")),
                        gap_opens=0,
                        query_start=int(hsp.findtext("Hsp_query-from", "0")),
                        query_end=int(hsp.findtext("Hsp_query-to", "0")),
                        subject_start=int(hsp.findtext("Hsp_hit-from", "0")),
                        subject_end=int(hsp.findtext("Hsp_hit-to", "0")),
                        evalue=float(hsp.findtext("Hsp_evalue", "999")),
                        bit_score=float(hsp.findtext("Hsp_bit-score", "0")),
                        subject_description=hit_def,
                    ))
                except (ValueError, TypeError):
                    continue
                break  # only first HSP per hit

    if progress_cb:
        progress_cb(f"[green]{len(hits)} NCBI BlastP hits found.[/]")
    return hits


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
    stop_count: int = 1  # number of consecutive in-frame stop codons


def _six_frame_proteins(nucleotide: str, seq_id: str) -> list[tuple[str, str]]:
    """Return (id, protein_seq) for all ORFs that start with M and end at a
    stop codon, with length >= 30 aa."""
    from Bio.Seq import Seq
    seq = Seq(nucleotide.upper())
    results = []
    for strand, nuc in (("+", seq), ("-", seq.reverse_complement())):
        for frame in range(3):
            # Trim to a codon boundary so translate() doesn't warn about
            # partial codons.
            frame_len = (len(nuc) - frame) // 3 * 3
            trans = str(nuc[frame:frame + frame_len].translate(to_stop=False))
            segments = trans.split("*")
            aa_offset = 0
            for i, seg in enumerate(segments):
                has_stop = i < len(segments) - 1
                if has_stop:
                    # Find all M positions; each starts a candidate ORF ending at the stop
                    orf_idx = 0
                    seg_len = len(seg)
                    m_pos = seg.find("M")
                    while m_pos != -1:
                        cand_len = seg_len - m_pos
                        if cand_len >= 30:
                            results.append((
                                f"{seq_id}_{strand}f{frame+1}_orf{i}m{orf_idx}",
                                seg[m_pos:],
                            ))
                            orf_idx += 1
                        m_pos = seg.find("M", m_pos + 1)
                aa_offset += len(seg) + 1
    return results


def _six_frame_orf_coords(nucleotide: str, seq_id: str, min_aa: int = 30) -> list[ORFCoord]:
    """Return ORFs that start with ATG (M) and end at a stop codon,
    with their nucleotide-space coordinates on the original sequence."""
    from Bio.Seq import Seq
    seq = Seq(nucleotide.upper())
    seq_len = len(seq)
    coords: list[ORFCoord] = []
    for strand, nuc in (("+", seq), ("-", seq.reverse_complement())):
        for frame_idx in range(3):
            frame_len = (len(nuc) - frame_idx) // 3 * 3
            trans = str(nuc[frame_idx:frame_idx + frame_len].translate(to_stop=False))
            segments = trans.split("*")
            aa_offset = 0
            for i, seg in enumerate(segments):
                has_stop = i < len(segments) - 1
                if has_stop:
                    # Count consecutive in-frame stop codons (empty segments)
                    n_stops = 1
                    for j in range(i + 1, len(segments) - 1):
                        if len(segments[j]) == 0:
                            n_stops += 1
                        else:
                            break
                    orf_idx = 0
                    seg_len = len(seg)
                    m_pos = seg.find("M")
                    while m_pos != -1:
                        candidate = seg[m_pos:]
                        if len(candidate) >= min_aa:
                            cand_aa_offset = aa_offset + m_pos
                            strand_nt_start = frame_idx + cand_aa_offset * 3
                            # +3 per stop codon to include consecutive stops
                            strand_nt_end = strand_nt_start + len(candidate) * 3 + 3 * n_stops
                            strand_nt_end = min(strand_nt_end, len(nuc))
                            if strand == "+":
                                nt_start = strand_nt_start
                                nt_end = strand_nt_end
                            else:
                                nt_start = seq_len - strand_nt_end
                                nt_end = seq_len - strand_nt_start
                            orf_id = f"{seq_id}_{strand}f{frame_idx+1}_orf{i}m{orf_idx}"
                            coords.append(ORFCoord(
                                orf_id=orf_id, strand=strand, frame=frame_idx + 1,
                                nt_start=nt_start, nt_end=nt_end,
                                aa_length=len(candidate), sequence=candidate,
                                stop_count=n_stops,
                            ))
                            orf_idx += 1
                        m_pos = seg.find("M", m_pos + 1)
                aa_offset += len(seg) + 1
    return coords


# Cache keyed by (seq_id, length, short content fingerprint).
# The fingerprint is a cheap hash of the first/middle/last 64 bases, which
# is effectively collision-free for distinct transcripts while still much
# cheaper than hashing a multi-kb sequence on every call.
_longest_orf_cache: OrderedDict[tuple[str, int, int], ORFCoord | None] = OrderedDict()
_LONGEST_ORF_CACHE_MAX = 4096


def _seq_fingerprint(seq: str) -> int:
    n = len(seq)
    if n <= 192:
        return hash(seq)
    mid = n // 2
    return hash((seq[:64], seq[mid:mid + 64], seq[-64:]))


# Minimal codon table for translating a single winning ORF after the regex
# scanner has picked it. Kept small and local — no general-purpose translator.
_ORF_CODON_TABLE: dict[str, str] = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V",
    "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}


def _translate_orf_dna(dna: str) -> str:
    """Translate a single contiguous ORF DNA region to its aa sequence.
    Excludes the terminating stop codon. Unknown codons (e.g. containing N)
    produce 'X'."""
    table = _ORF_CODON_TABLE
    aa: list[str] = []
    for i in range(0, len(dna) - 2, 3):
        codon = dna[i:i + 3]
        letter = table.get(codon, "X")
        if letter == "*":
            break
        aa.append(letter)
    return "".join(aa)


def _find_longest_orf(nucleotide: str, seq_id: str) -> ORFCoord | None:
    """Find the single longest M-initiated ORF across all 6 frames.

    Uses the regex codon scanner (~10x faster than `_six_frame_orf_coords`
    which routes every codon through Biopython's `Seq.translate`). The
    ground-truth implementation is retained in `_six_frame_orf_coords`;
    `tests/test_dna_sanity.py` cross-validates this fast path against it
    on hundreds of random sequences per run.
    """
    key = (seq_id, len(nucleotide), _seq_fingerprint(nucleotide))
    if key in _longest_orf_cache:
        _longest_orf_cache.move_to_end(key)
        return _longest_orf_cache[key]

    upper = nucleotide.upper()
    rc = upper.translate(_RC_TABLE)[::-1]
    seq_len = len(upper)

    best_strand = ""
    best_frame = -1
    best_strand_m = -1
    best_strand_stop = -1  # position of the terminating stop on the strand
    best_length = 0

    for strand_label, strand_seq in (("+", upper), ("-", rc)):
        # Partition matches into reading frames in a single pass.
        frames: list[list[tuple[int, str]]] = [[], [], []]
        for m in _CODON_SCAN_RE.finditer(strand_seq):
            p = m.start()
            frames[p % 3].append((p, m.group(1)))
        for frame_idx, frame_hits in enumerate(frames):
            first_atg = -1
            for p, codon in frame_hits:
                if codon == "ATG":
                    if first_atg == -1:
                        first_atg = p
                else:  # stop codon
                    if first_atg != -1:
                        length = (p - first_atg) // 3
                        if length > best_length:
                            best_length = length
                            best_strand = strand_label
                            best_frame = frame_idx
                            best_strand_m = first_atg
                            best_strand_stop = p
                        first_atg = -1

    if best_length < 30:
        _longest_orf_cache[key] = None
        if len(_longest_orf_cache) > _LONGEST_ORF_CACHE_MAX:
            _longest_orf_cache.popitem(last=False)
        return None

    # Extract the ORF's DNA (M through stop, not including stop) and translate.
    strand_seq = upper if best_strand == "+" else rc
    orf_dna = strand_seq[best_strand_m:best_strand_stop]
    aa_sequence = _translate_orf_dna(orf_dna)

    # Count consecutive in-frame stop codons immediately after the first stop.
    n_stops = 1
    probe = best_strand_stop + 3
    strand_stops = ("TAA", "TAG", "TGA")
    while probe + 3 <= len(strand_seq) and strand_seq[probe:probe + 3] in strand_stops:
        n_stops += 1
        probe += 3

    # Convert strand-local coordinates to the original sequence.
    strand_nt_start = best_strand_m
    strand_nt_end = best_strand_stop + 3 * n_stops
    strand_nt_end = min(strand_nt_end, len(strand_seq))
    if best_strand == "+":
        nt_start = strand_nt_start
        nt_end = strand_nt_end
    else:
        nt_start = seq_len - strand_nt_end
        nt_end = seq_len - strand_nt_start

    orf_id = f"{seq_id}_{best_strand}f{best_frame + 1}_m{best_strand_m}"
    result = ORFCoord(
        orf_id=orf_id,
        strand=best_strand,
        frame=best_frame + 1,
        nt_start=nt_start,
        nt_end=nt_end,
        aa_length=best_length,
        sequence=aa_sequence,
        stop_count=n_stops,
    )
    _longest_orf_cache[key] = result
    if len(_longest_orf_cache) > _LONGEST_ORF_CACHE_MAX:
        _longest_orf_cache.popitem(last=False)
    return result


_RC_TABLE = str.maketrans("ACGTN", "TGCAN")


# Regex that finds every start and stop codon position in both strands.
# The lookahead `(?=...)` is required so overlapping matches across frames
# aren't hidden — e.g. "TAATG" has a TAA at 0 and an ATG at 2 which
# belong to different reading frames.
_CODON_SCAN_RE = re.compile(r"(?=(ATG|TAA|TAG|TGA))")


def _longest_orf_aa_length(nucleotide: str, min_aa: int = 30) -> int:
    """Fast path: return the length (in aa) of the longest M-initiated ORF
    across all 6 frames, or 0 if none >= min_aa.

    Scans the DNA directly for start/stop codons via a single compiled
    regex per strand — roughly 4x faster than translating the full protein
    sequence and walking it, and orders of magnitude faster than building
    ORFCoord objects via `_six_frame_orf_coords`.
    """
    upper = nucleotide.upper()
    rc = upper.translate(_RC_TABLE)[::-1]
    best = 0
    for strand in (upper, rc):
        # Partition matches into reading frames in a single pass.
        frames: list[list[tuple[int, str]]] = [[], [], []]
        for m in _CODON_SCAN_RE.finditer(strand):
            p = m.start()
            frames[p % 3].append((p, m.group(1)))
        for frame_hits in frames:
            first_atg = -1
            for p, codon in frame_hits:
                if codon == "ATG":
                    if first_atg == -1:
                        first_atg = p
                else:  # stop codon
                    if first_atg != -1:
                        length = (p - first_atg) // 3
                        if length > best:
                            best = length
                        first_atg = -1
    return best if best >= min_aa else 0


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
    # Align protein track under the CDS region of the transcript track.
    # Protein aa positions map into the same columns as the CDS nucleotides.
    protein_len = best_orf.aa_length
    cds_col_start = pos_to_col(best_orf.nt_start, seq_len)
    cds_col_end = pos_to_col(best_orf.nt_end, seq_len)
    cds_col_end = max(cds_col_end, cds_col_start + 1)
    cds_track_w = cds_col_end - cds_col_start

    def aa_to_col(aa_pos: int) -> int:
        """Map an amino acid position to a column aligned under the CDS."""
        if protein_len == 0:
            return cds_col_start
        return cds_col_start + min(int(aa_pos / protein_len * cds_track_w), cds_track_w)

    result.append("\n")
    result.append(f"  Protein", style="bold bright_white")
    result.append(f"  ({protein_len:,} aa)\n", style="dim")
    result.append("─" * rule_w + "\n", style="dim")
    result.append(_render_scale(label_w, track_w, seq_len, " nt"))

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
        # Map domain positions into CDS-aligned columns
        for aa_start, aa_end, family, _acc, dcolor, _ev in domain_info:
            c_start = aa_to_col(aa_start)
            c_end = aa_to_col(aa_end)
            c_end = max(c_end, c_start + 1)
            for c in range(c_start, min(c_end + 1, track_w + 1)):
                track[c] = "█"

        # Render with domain colors (simplified: use first domain color per position)
        # Build a color map per column
        col_color = ["bright_black"] * (track_w + 1)
        for aa_start, aa_end, _fam, _acc, dcolor, _ev in domain_info:
            c_start = aa_to_col(aa_start)
            c_end = aa_to_col(aa_end)
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
            c_start = aa_to_col(aa_start)
            c_end = aa_to_col(aa_end)
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


_hmm_cpus = os.cpu_count() or 4

# Threading event used to abort long-running HMM scans (e.g. on app quit).
_hmm_cancel = threading.Event()


class HMMCancelled(Exception):
    """Raised when an HMM scan is aborted."""


# ── HMM database cache ──────────────────────────────────────────────────────
# Avoids reloading ~20k Pfam HMMs on every scan.

class _HMMCache:
    """Cache pressed/loaded HMMs so repeated scans skip the file-loading step."""

    def __init__(self) -> None:
        self._path: str = ""
        self._hmms: list | None = None
        self._lock = threading.Lock()

    def get(self, db_path: str) -> list:
        with self._lock:
            if self._path == db_path and self._hmms is not None:
                return self._hmms
        # Load outside the lock to avoid blocking concurrent callers
        _log.info("Loading HMM database %s …", db_path)
        import pyhmmer
        with pyhmmer.plan7.HMMFile(db_path) as hmm_file:
            hmms = list(hmm_file)
        with self._lock:
            # Re-check in case another thread loaded while we were reading
            if self._path == db_path and self._hmms is not None:
                return self._hmms
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
    """Return the path to the local Pfam HMM if it exists and looks valid, else None."""
    hmm_path = dest_dir / "Pfam-A.hmm"
    if hmm_path.exists():
        # Pfam-A.hmm is typically >1 GB; a tiny file means interrupted download
        if hmm_path.stat().st_size < 1_000_000:
            _log.warning("Pfam HMM file appears truncated (%d bytes), ignoring", hmm_path.stat().st_size)
            return None
        return str(hmm_path)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Sequence viewer widget
# ══════════════════════════════════════════════════════════════════════════════

# Base colors chosen so GC content is visible at a glance AND the scheme
# remains legible for users with red-green color vision deficiency
# (~8% of men). The primary AT-vs-GC signal uses the canonical
# colorblind-safe blue/orange pairing; within each pair a hue shift
# distinguishes individual bases without breaking the family reading.
#
#   A = blue        (#0087ff)  \  cool — AT family
#   T = aquamarine  (#87ffd7)  /  (green-shifted for distance from A)
#   C = orange      (#ff8700)  \  warm — GC family
#   G = red         (#ff0000)  /
#
# T is aquamarine1 rather than pure cyan so it's more distinguishable
# from A (dodger_blue1) while still reading as "cool" — the AT-vs-GC
# pair-grouping is preserved and an AT-rich region still shows as a
# uniformly cool-toned band.
#
# All four are fixed xterm-256 color names (dodger_blue1, aquamarine1,
# dark_orange, red1). The basic ANSI 16-color names (bright_blue,
# bright_cyan, bright_red) are avoided because terminal themes remap
# them freely — bright_blue in particular shows up as violet/purple
# in many common palettes (Solarized, macOS Terminal default, etc.).
# 256-color names are RGB-anchored and render consistently.
_BASE_COLORS = {
    "A": "bold dodger_blue1",
    "T": "bold aquamarine1",
    "U": "bold aquamarine1",
    "C": "bold dark_orange",
    "G": "bold red1",
    "N": "dim white",
}
_MAX_DISPLAY_BASES = 10_000


def colorize_sequence(seq: str, width: int = 60) -> Text:
    truncated = len(seq) > _MAX_DISPLAY_BASES
    display_seq = seq[:_MAX_DISPLAY_BASES] if truncated else seq

    result = Text(no_wrap=False)
    for i in range(0, len(display_seq), width):
        chunk = display_seq[i : i + width]
        run_base = chunk[0].upper()
        run_chars: list[str] = [chunk[0]]
        for base in chunk[1:]:
            upper = base.upper()
            if upper == run_base:
                run_chars.append(base)
            else:
                result.append("".join(run_chars), style=_BASE_COLORS.get(run_base, "white"))
                run_base = upper
                run_chars = [base]
        result.append("".join(run_chars), style=_BASE_COLORS.get(run_base, "white"))
        result.append("\n")

    if truncated:
        result.append(
            f"\n  … {len(seq) - _MAX_DISPLAY_BASES:,} more bases not shown\n",
            style="dim italic",
        )
    return result


@dataclass
class SeqLineInfo:
    """Metadata for one rendered line in the annotated sequence view."""
    line_type: str        # "header", "feat", "pfam", "dna", "aa", "blank"
    chunk_start: int      # nucleotide offset of the chunk this line belongs to
    chunk_len: int        # number of nucleotides in the chunk


@dataclass
class SeqFeatureRegion:
    """A clickable feature region in nucleotide space."""
    name: str
    nt_start: int
    nt_end: int           # exclusive
    aa_seq: str = ""      # amino acid sequence for this feature (N-term to C-term)


@dataclass
class SeqRenderResult:
    """Result of colorize_sequence_annotated."""
    text: Text
    line_map: list[SeqLineInfo]
    features: list[SeqFeatureRegion]


def _aa_codon_start(orf: ORFCoord, aa_idx: int) -> int:
    """Return the nucleotide position of the first base in codon aa_idx."""
    if orf.strand == "+":
        return orf.nt_start + aa_idx * 3
    return orf.nt_end - (aa_idx + 1) * 3


def _build_aa_track(
    orf: ORFCoord, n: int,
    aa_at: list[str | None], aa_color: list[str],
    base_override: list[str | None],
) -> None:
    """Fill amino acid and start/stop codon arrays for the CDS."""
    cds_color = _FRAME_COLORS.get((orf.strand, orf.frame), "bright_cyan")

    for aa_idx in range(orf.aa_length):
        codon_start = _aa_codon_start(orf, aa_idx)
        if codon_start < 0 or codon_start + 2 >= n:
            continue
        mid = codon_start + 1
        aa_at[mid] = orf.sequence[aa_idx]
        aa_color[mid] = cds_color

    # Highlight start codon
    start_codon_pos = _aa_codon_start(orf, 0)
    if 0 <= start_codon_pos and start_codon_pos + 2 < n:
        for k in range(3):
            base_override[start_codon_pos + k] = "bold bright_white on green"

    # Stop codons
    for si in range(orf.stop_count):
        stop_pos = (orf.nt_end - (orf.stop_count - si) * 3) if orf.strand == "+" else (orf.nt_start + si * 3)
        if 0 <= stop_pos and stop_pos + 2 < n:
            for k in range(3):
                base_override[stop_pos + k] = "bold bright_white on red"
            mid = stop_pos + 1
            aa_at[mid] = "*"
            aa_color[mid] = "bold bright_red"


def _build_feature_track(
    orf: ORFCoord, n: int,
    feat_ch: list[str | None], feat_color: list[str],
) -> None:
    """Fill CDS arrow track arrays (SnapGene-style)."""
    cds_color = _FRAME_COLORS.get((orf.strand, orf.frame), "bright_cyan")
    cds_lo, cds_hi = orf.nt_start, orf.nt_end
    cds_nt_len = cds_hi - cds_lo

    for pos in range(max(0, cds_lo), min(n, cds_hi)):
        feat_ch[pos] = "▓"
        feat_color[pos] = cds_color

    cds_label = "CDS"
    if cds_nt_len >= len(cds_label) + 4:
        label_start = cds_lo + (cds_nt_len - len(cds_label)) // 2
        for ci, ch in enumerate(cds_label):
            lp = label_start + ci
            if 0 <= lp < n:
                feat_ch[lp] = ch
                feat_color[lp] = f"bold {cds_color}"

    arrow_tip = "▶" if orf.strand == "+" else "◀"
    if orf.strand == "+":
        tip_pos = min(cds_hi - 1, n - 1)
        if 0 <= tip_pos:
            feat_ch[tip_pos] = arrow_tip
            feat_color[tip_pos] = f"bold reverse {cds_color}"
    else:
        tip_pos = max(cds_lo, 0)
        if tip_pos < n:
            feat_ch[tip_pos] = arrow_tip
            feat_color[tip_pos] = f"bold reverse {cds_color}"


def _build_pfam_track(
    orf: ORFCoord, hits: list[HmmerHit], n: int,
    pfam_label: list[str | None], pfam_color: list[str],
) -> tuple[list[HmmerHit], dict[str, str]]:
    """Fill Pfam domain annotation arrays. Returns (sorted_hits, domain_colors)."""
    sorted_hits = sorted(hits, key=lambda h: h.evalue)
    domain_colors: dict[str, str] = {}
    cidx = 0
    for h in sorted_hits:
        if h.target_name not in domain_colors:
            domain_colors[h.target_name] = _DOMAIN_PALETTE[cidx % len(_DOMAIN_PALETTE)]
            cidx += 1

    pfam_claimed: list[bool] = [False] * n
    for h in sorted_hits:
        dcolor = domain_colors[h.target_name]
        aa_start = h.ali_from - 1
        aa_end = h.ali_to
        nt_dom_start = _aa_codon_start(orf, aa_start)
        nt_dom_end = _aa_codon_start(orf, aa_end - 1) + 3
        if nt_dom_start > nt_dom_end:
            nt_dom_start, nt_dom_end = nt_dom_end, nt_dom_start
        nt_dom_start = max(0, nt_dom_start)
        nt_dom_end = min(n, nt_dom_end)

        has_unclaimed = any(not pfam_claimed[p] for p in range(nt_dom_start, nt_dom_end))
        if not has_unclaimed:
            continue

        name = h.target_name
        dom_nt_len = nt_dom_end - nt_dom_start
        if dom_nt_len >= len(name) + 2:
            pad = dom_nt_len - len(name)
            pad_l = pad // 2
            for j in range(dom_nt_len):
                pos = nt_dom_start + j
                if pos >= n:
                    break
                if pfam_claimed[pos]:
                    continue
                pfam_claimed[pos] = True
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
                if pfam_claimed[pos]:
                    continue
                pfam_claimed[pos] = True
                pfam_label[pos] = "━"
                pfam_color[pos] = dcolor

    return sorted_hits, domain_colors


def colorize_sequence_annotated(
    seq: str,
    orf: ORFCoord | None = None,
    hits: list[HmmerHit] | None = None,
    width: int = 60,
    highlight_range: tuple[int, int] | None = None,
    aa_highlight_range: tuple[int, int] | None = None,
    focus_range: tuple[int, int] | None = None,
) -> SeqRenderResult:
    """Render DNA with CDS amino-acid translation and Pfam domain tracks.

    highlight_range: strong blue-background highlight over an arbitrary
      window (used when navigating from a BLAST hit). Out-of-range bases
      are dimmed.
    focus_range: soft focus — bases inside the range keep their normal
      per-base ACGT colors; bases outside the range are dimmed to gray.
      Used when the user clicks the CDS arrow or AA translation line to
      "focus" on an ORF without losing the per-base color information
      inside the feature.
    """
    truncated = len(seq) > _MAX_DISPLAY_BASES
    display_seq = seq[:_MAX_DISPLAY_BASES] if truncated else seq
    prefix_w = 0
    n = len(display_seq)

    # Per-position annotation arrays
    aa_at: list[str | None] = [None] * n
    aa_color: list[str] = [""] * n
    pfam_label: list[str | None] = [None] * n
    pfam_color: list[str] = [""] * n
    feat_ch: list[str | None] = [None] * n
    feat_color: list[str] = [""] * n
    base_override: list[str | None] = [None] * n

    sorted_hits: list[HmmerHit] = []
    domain_colors: dict[str, str] = {}

    if orf:
        _build_aa_track(orf, n, aa_at, aa_color, base_override)
        _build_feature_track(orf, n, feat_ch, feat_color)
        if hits:
            sorted_hits, domain_colors = _build_pfam_track(orf, hits, n, pfam_label, pfam_color)

    # ── Assemble output ──────────────────────────────────────────────────
    # Build the entire output as a single plain string with pre-offset spans,
    # then construct ONE Text at the end. This avoids thousands of per-run
    # Text.append calls (each of which invokes Rich's strip_control_codes
    # validation) — benchmarks show ~2-3x speedup on the assembly phase.
    from rich.text import Span
    plain_parts: list[str] = []
    spans: list[Span] = []
    offset = 0
    line_map: list[SeqLineInfo] = []
    features: list[SeqFeatureRegion] = []
    cur_line = 0

    def _emit(text: str, style: str = "") -> None:
        nonlocal offset
        if not text:
            return
        plain_parts.append(text)
        if style:
            spans.append(Span(offset, offset + len(text), style))
        offset += len(text)

    def _flush_line(chars: list[str], styles: list[str]) -> None:
        """RLE the chars/styles arrays and emit a line + trailing newline."""
        n_local = len(chars)
        if n_local == 0:
            _emit("\n")
            return
        k = 0
        while k < n_local:
            run_style = styles[k]
            start = k
            while k < n_local and styles[k] == run_style:
                k += 1
            _emit("".join(chars[start:k]), run_style)
        _emit("\n")

    def _flush_line_str(plain: str, styles: list[str]) -> None:
        """Fast path for lines where the plain text is already a string.
        Avoids the per-run `"".join(chars[start:k])` list-slice allocation
        that _flush_line does. Used for the DNA line which is the single
        hottest emitter in the render.

        The spans are built by walking `styles` and slicing `plain`
        directly — no intermediate list construction.
        """
        n_local = len(styles)
        if n_local == 0:
            _emit("\n")
            return
        # Inlined emit loop — avoids the function-call overhead of
        # calling _emit once per run when there can be 75+ runs per line.
        nonlocal offset
        local_plain_parts = plain_parts
        local_spans = spans
        k = 0
        while k < n_local:
            run_style = styles[k]
            start = k
            while k < n_local and styles[k] == run_style:
                k += 1
            run_text = plain[start:k]
            local_plain_parts.append(run_text)
            if run_style:
                local_spans.append(Span(offset, offset + (k - start), run_style))
            offset += k - start
        local_plain_parts.append("\n")
        offset += 1

    # Header
    if orf:
        cds_color = _FRAME_COLORS.get((orf.strand, orf.frame), "bright_cyan")
        _emit("  CDS: ", "dim")
        _emit(f"{orf.nt_start+1}–{orf.nt_end}", f"bold {cds_color}")
        _emit(f" ({orf.aa_length} aa, {orf.strand}f{orf.frame})", f"dim {cds_color}")
        _emit("  ", "dim")
        _emit(" ATG ", "bold bright_white on green")
        _emit(" ", "dim")
        _emit(" STOP ", "bold bright_white on red")
        if hits:
            _emit("  Pfam: ", "dim")
            for fam, dcol in domain_colors.items():
                _emit(f"━━ {fam} ", f"bold {dcol}")
        _emit("\n\n")
        line_map.append(SeqLineInfo("header", 0, 0))
        line_map.append(SeqLineInfo("header", 0, 0))
        cur_line += 2
        features.append(SeqFeatureRegion("CDS", orf.nt_start, orf.nt_end, aa_seq=orf.sequence))

    if orf and hits:
        for h in sorted_hits:
            aa_start = h.ali_from - 1
            aa_end = h.ali_to
            nt_dom_start = orf.nt_start + aa_start * 3 if orf.strand == "+" else orf.nt_end - aa_end * 3
            nt_dom_end = orf.nt_start + aa_end * 3 if orf.strand == "+" else orf.nt_end - aa_start * 3
            if nt_dom_start > nt_dom_end:
                nt_dom_start, nt_dom_end = nt_dom_end, nt_dom_start
            dom_aa = orf.sequence[aa_start:aa_end]
            features.append(SeqFeatureRegion(h.target_name, max(0, nt_dom_start), min(n, nt_dom_end), aa_seq=dom_aa))

    # Precompute flat per-position style arrays ONCE for each line type.
    # The chunk loop then just slices these arrays instead of rebuilding
    # them character by character every chunk — previously the dominant
    # cost (~1M list.appends per render for a 5 kb transcript).
    base_colors = _BASE_COLORS

    # DNA line style strategy:
    # - highlight_range (BLAST nav): strong blue-background for in-range,
    #   "dim" everywhere else.
    # - focus_range (click on feat/aa line of an ORF): per-base ACGT colors
    #   inside the range, "dim" outside. Preserves GC-at-a-glance inside
    #   the focused feature while muting the rest of the transcript.
    # - default (no highlight, no focus): per-base ACGT colors everywhere,
    #   with start/stop codon overrides punched through via base_override.
    dna_styles: list[str] = [""] * n
    if highlight_range:
        hl_lo, hl_hi = highlight_range
        hl_style = "bold bright_white on dark_blue"
        for pos in range(n):
            if hl_lo <= pos < hl_hi:
                dna_styles[pos] = hl_style
            else:
                dna_styles[pos] = "dim"
    elif focus_range:
        fr_lo, fr_hi = focus_range
        for pos in range(n):
            if fr_lo <= pos < fr_hi:
                override = base_override[pos]
                if override is not None:
                    dna_styles[pos] = override
                else:
                    dna_styles[pos] = base_colors.get(display_seq[pos].upper(), "white")
            else:
                dna_styles[pos] = "dim"
    else:
        for pos in range(n):
            override = base_override[pos]
            if override is not None:
                dna_styles[pos] = override
            else:
                dna_styles[pos] = base_colors.get(display_seq[pos].upper(), "white")

    # AA line: only populated positions have chars/styles; others are spaces.
    if orf:
        aa_chars: list[str] = [" "] * n
        aa_styles: list[str] = [""] * n
        aa_hl = aa_highlight_range
        aa_hl_style = "bold bright_white on dark_magenta"
        for pos in range(n):
            glyph = aa_at[pos]
            if glyph is None:
                continue
            aa_chars[pos] = glyph
            if aa_hl:
                if aa_hl[0] <= pos < aa_hl[1]:
                    aa_styles[pos] = aa_hl_style
                else:
                    aa_styles[pos] = "dim"
            else:
                aa_styles[pos] = aa_color[pos]
    else:
        aa_chars = []
        aa_styles = []

    # Feature (CDS arrow) line: most positions are spaces.
    if orf:
        feat_line_chars: list[str] = [" "] * n
        feat_line_styles: list[str] = [""] * n
        for pos in range(n):
            ch = feat_ch[pos]
            if ch is None:
                continue
            feat_line_chars[pos] = ch
            if ch in ("▓", "━"):
                feat_line_styles[pos] = feat_color[pos]
            else:
                feat_line_styles[pos] = f"bold {feat_color[pos]}"
    else:
        feat_line_chars = []
        feat_line_styles = []

    # Pfam domain label line: most positions are spaces.
    if hits and orf:
        pfam_line_chars: list[str] = [" "] * n
        pfam_line_styles: list[str] = [""] * n
        for pos in range(n):
            ch = pfam_label[pos]
            if ch is None:
                continue
            if ch == "━":
                pfam_line_chars[pos] = "▄"
                pfam_line_styles[pos] = pfam_color[pos]
            else:
                pfam_line_chars[pos] = ch
                pfam_line_styles[pos] = f"bold {pfam_color[pos]}"
    else:
        pfam_line_chars = []
        pfam_line_styles = []

    # Bitmaps of which chunks need feat / pfam / aa lines (avoids per-chunk
    # `any(...)` scans over the sub-track arrays).
    def _chunks_with_content(chars_arr: list[str]) -> set[int]:
        if not chars_arr:
            return set()
        result = set()
        for pos in range(n):
            if chars_arr[pos] != " ":
                result.add(pos // width)
        return result

    feat_chunks = _chunks_with_content(feat_line_chars)
    pfam_chunks = _chunks_with_content(pfam_line_chars)
    aa_chunks = _chunks_with_content(aa_chars)

    for chunk_idx, i in enumerate(range(0, n, width)):
        chunk_end = min(i + width, n)
        chunk_len = chunk_end - i

        # CDS feature arrow
        if orf and chunk_idx in feat_chunks:
            _flush_line(feat_line_chars[i:chunk_end], feat_line_styles[i:chunk_end])
            line_map.append(SeqLineInfo("feat", i, chunk_len))
            cur_line += 1

        # Pfam domain track
        if hits and orf and chunk_idx in pfam_chunks:
            _flush_line(pfam_line_chars[i:chunk_end], pfam_line_styles[i:chunk_end])
            line_map.append(SeqLineInfo("pfam", i, chunk_len))
            cur_line += 1

        # DNA line — use the fast string-slice path
        _flush_line_str(display_seq[i:chunk_end], dna_styles[i:chunk_end])
        line_map.append(SeqLineInfo("dna", i, chunk_len))
        cur_line += 1

        # Amino acid translation line
        if orf and chunk_idx in aa_chunks:
            _flush_line(aa_chars[i:chunk_end], aa_styles[i:chunk_end])
            line_map.append(SeqLineInfo("aa", i, chunk_len))
            cur_line += 1

        _emit("\n")
        line_map.append(SeqLineInfo("blank", i, chunk_len))
        cur_line += 1

    if truncated:
        _emit(
            f"\n  … {len(seq) - _MAX_DISPLAY_BASES:,} more bases not shown\n",
            "dim italic",
        )

    result = Text("".join(plain_parts), spans=spans, no_wrap=False)
    return SeqRenderResult(text=result, line_map=line_map, features=features)


class SequenceViewer(ScrollableContainer):
    DEFAULT_CSS = """
    SequenceViewer { height: 1fr; }
    SequenceViewer #seq-info { background: $surface; padding: 0 1; height: auto; }
    SequenceViewer #seq-cds-btn { height: auto; padding: 0 1; }
    SequenceViewer #seq-cds-btn.hidden { display: none; }
    SequenceViewer #seq-cds-area { display: none; height: auto; max-height: 12; padding: 0 1; }
    SequenceViewer #seq-cds-area.visible { display: block; }
    SequenceViewer #seq-body { padding: 0 1; height: auto; }
    SequenceViewer #seq-goto-row { height: 3; padding: 0 1; background: $surface; }
    SequenceViewer #seq-goto-row Label { width: auto; padding: 0 1 0 0; }
    SequenceViewer #seq-goto-input { width: 16; }
    """

    transcript: reactive[Transcript | None] = reactive(None)
    _cds_dna: str = ""  # raw CDS DNA for clipboard copy
    _line_map: list[SeqLineInfo] = []
    _features: list[SeqFeatureRegion] = []
    _highlight: tuple[int, int] | None = None      # strong BLAST nav highlight
    _aa_highlight: tuple[int, int] | None = None   # AA line highlight
    _aa_highlight_seq: str = ""                    # for clipboard (N→C)
    _focus_range: tuple[int, int] | None = None    # soft focus — dim bases outside this range
    _last_orf: ORFCoord | None = None
    _last_hits: list[HmmerHit] = []
    _last_width: int = 60
    _RENDER_CACHE_MAX: int = 64
    _last_shown_id: str = ""

    def watch_transcript(self, t: Transcript | None) -> None:
        new_id = t.id if t is not None else ""
        if new_id == self._last_shown_id:
            return
        self._last_shown_id = new_id
        self._highlight = None
        self._aa_highlight = None
        self._aa_highlight_seq = ""
        self._focus_range = None
        if t:
            self.show_transcript(t)
        else:
            self.query_one("#seq-info", Static).update("Select a transcript from the list.")
            self.query_one("#seq-body", Static).update("")
            self._cds_dna = ""
            self.query_one("#seq-cds-btn", Static).add_class("hidden")
            self.query_one("#seq-cds-area", TextArea).remove_class("visible")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._seq_render_cache: OrderedDict[tuple, SeqRenderResult] = OrderedDict()

    def compose(self) -> ComposeResult:
        yield Static("Select a transcript from the list.", id="seq-info")
        with Horizontal(id="seq-goto-row"):
            yield Label("Go to position:")
            yield Input(placeholder="e.g. 1250", id="seq-goto-input", type="integer")
        yield Static("", id="seq-cds-btn", classes="hidden")
        yield TextArea("", id="seq-cds-area", read_only=True)
        yield Static("", id="seq-body")

    def refresh_annotations(self) -> None:
        """Re-render the sequence with current scan data from the HmmerPanel."""
        t = self.transcript
        if t:
            self.show_transcript(t)

    @on(Input.Submitted, "#seq-goto-input")
    def _goto_position(self, event: Input.Submitted) -> None:
        """Scroll the sequence view to the given nucleotide position."""
        t = self.transcript
        if not t or not event.value.strip():
            return
        try:
            pos = int(event.value.strip()) - 1  # 1-based → 0-based
        except ValueError:
            return
        pos = max(0, min(pos, t.length - 1))
        # Find the line in line_map that contains this position
        target_line = 0
        for idx, info in enumerate(self._line_map):
            if info.line_type == "dna" and info.chunk_start <= pos < info.chunk_start + info.chunk_len:
                target_line = idx
                break
        # Each line is roughly 1 row in the Static; scroll the parent container
        body = self.query_one("#seq-body", Static)
        # Estimate y offset: each line_map entry ≈ 1 terminal row
        body.scroll_to(0, max(0, target_line - 3), animate=False)

    _last_container_w: int = 0

    def on_resize(self, event) -> None:
        """Re-render sequence when terminal/widget size changes."""
        w = self.size.width
        if w == self._last_container_w:
            return  # width unchanged — skip re-render
        self._last_container_w = w
        t = self.transcript
        if t:
            self.show_transcript(t)

    _render_seq_id: str = ""  # track which transcript render is in flight

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
                    f"[bold dodger_blue1]A[/]:{counts['A']}  "
                    f"[bold dark_orange]C[/]:{counts['C']}  "
                    f"[bold red1]G[/]:{counts['G']}  "
                    f"[bold aquamarine1]T[/]:{counts['T']}  "
                    f"[dim]N[/]:{counts['N']}"
                ),
            )
            info.update(grid)

            cds_btn = self.query_one("#seq-cds-btn", Static)
            cds_area = self.query_one("#seq-cds-area", TextArea)
            cds_area.remove_class("visible")
            cds_btn.add_class("hidden")

            # Capture values that need main-thread context
            body_padding = 2
            scrollbar_w = 2
            container_w = self.size.width or 0
            overhead = body_padding + scrollbar_w
            seq_width = (container_w - overhead) if container_w > overhead else 60

            scan_cache: dict | None = None
            try:
                hmmer = self.app.query_one("#hmmer-panel")
                scan_cache = getattr(hmmer, "_scan_cache", None)
            except Exception:
                pass

            # Clear body immediately so Textual doesn't render stale large content
            body.update(Text("Loading…", style="dim"))
            self._render_seq_id = t.id
            self._render_sequence_bg(t, seq_width, scan_cache)
        except Exception as exc:
            _log.exception("SequenceViewer.show_transcript failed: %s", exc)
            info.update(f"[red]Error: {exc}[/]")
            body.update("")

    @work(exclusive=True, thread=True, group="seq-render")
    def _render_sequence_bg(self, t: Transcript, seq_width: int, scan_cache: dict | None) -> None:
        """Heavy sequence rendering on a background thread."""
        scanned = scan_cache is not None and t.id in scan_cache
        _log.debug(
            "render_sequence_bg start id=%s length=%d width=%d scanned=%s",
            t.id, t.length, seq_width, scanned,
        )
        # Gather inputs
        orf = None
        hits: list[HmmerHit] = []
        try:
            if scanned:
                all_hits = scan_cache[t.id]
                hits = sorted(all_hits, key=lambda h: h.evalue)[:5]
                orf = _find_longest_orf(t.sequence, t.id)
        except Exception:
            pass

        # Build CDS DNA
        cds_dna = ""
        if orf:
            if orf.strand == "+":
                cds_dna = t.sequence[orf.nt_start:orf.nt_end]
            else:
                from Bio.Seq import Seq
                cds_dna = str(Seq(t.sequence[orf.nt_start:orf.nt_end]).reverse_complement())

        # Render (expensive)
        if orf:
            hit_key = tuple((h.target_name, h.ali_from, h.ali_to) for h in hits)
            cache_key = (
                t.id, seq_width, self._highlight, self._aa_highlight,
                self._focus_range, hit_key,
            )
            render = self._seq_render_cache.get(cache_key)
            if render is not None:
                self._seq_render_cache.move_to_end(cache_key)
            else:
                render = colorize_sequence_annotated(
                    t.sequence, orf=orf, hits=hits, width=seq_width,
                    highlight_range=self._highlight,
                    aa_highlight_range=self._aa_highlight,
                    focus_range=self._focus_range,
                )
                if len(self._seq_render_cache) >= self._RENDER_CACHE_MAX:
                    self._seq_render_cache.popitem(last=False)
                self._seq_render_cache[cache_key] = render
        else:
            render = None

        plain_text = None
        if not orf:
            plain_text = colorize_sequence(t.sequence, width=seq_width)

        # Apply results on main thread — bail if user already switched transcripts
        def _apply() -> None:
            if self._render_seq_id != t.id:
                return  # user clicked another transcript, discard
            self._cds_dna = cds_dna
            self._last_orf = orf
            self._last_hits = hits
            self._last_width = seq_width
            body = self.query_one("#seq-body", Static)
            if render is not None:
                self._line_map = render.line_map
                self._features = render.features
                body.update(render.text)
            else:
                self._line_map = []
                self._features = []
                body.update(plain_text)

        self.app.call_from_thread(_apply)

    def copy_cds_to_clipboard(self) -> None:
        """Copy the CDS DNA sequence to the system clipboard."""
        if self._cds_dna:
            self.app.copy_to_clipboard(self._cds_dna)
            self.app.notify(f"CDS DNA copied ({len(self._cds_dna)} bp)", severity="information")
        else:
            self.app.notify("No CDS detected for this transcript", severity="warning")

    def _clear_highlights(self) -> None:
        changed = (
            self._highlight is not None
            or self._aa_highlight is not None
            or self._focus_range is not None
        )
        self._highlight = None
        self._aa_highlight = None
        self._aa_highlight_seq = ""
        self._focus_range = None
        if changed:
            self.show_transcript(self.transcript)

    @on(events.Click, "#seq-body")
    def _on_seq_body_click(self, event: events.Click) -> None:
        """Click behavior in the sequence body:
          - feat/pfam line click on a feature → focus the feature (dim bases
            outside the feature range, keep per-base colors inside)
          - AA line click on a feature → focus + copy the feature's aa seq
          - DNA/blank/header click → clear focus, restore full per-base coloring
        """
        if not self._line_map or not self._features:
            return
        event.stop()
        y = event.y
        if y < 0 or y >= len(self._line_map):
            self._clear_highlights()
            return
        line_info = self._line_map[y]
        x = event.x
        # Account for #seq-body padding (0 1 = 1 char left)
        nt_pos = line_info.chunk_start + (x - 1)

        if line_info.line_type in ("feat", "pfam"):
            # Click on feature/pfam line → focus the ORF/domain it belongs to.
            for feat in self._features:
                if feat.nt_start <= nt_pos < feat.nt_end:
                    new_focus = (feat.nt_start, feat.nt_end)
                    if self._focus_range == new_focus and self._aa_highlight is None:
                        # Already focused on this feature — toggle off.
                        self._clear_highlights()
                    else:
                        self._focus_range = new_focus
                        self._highlight = None
                        self._aa_highlight = None
                        self._aa_highlight_seq = ""
                        self.show_transcript(self.transcript)
                    return
            self._clear_highlights()

        elif line_info.line_type == "aa":
            # Click on AA line → focus the feature AND highlight its aa run
            # for clipboard copy on Ctrl+C.
            for feat in self._features:
                if feat.nt_start <= nt_pos < feat.nt_end:
                    new_focus = (feat.nt_start, feat.nt_end)
                    if self._aa_highlight == new_focus:
                        self._clear_highlights()
                    else:
                        self._focus_range = new_focus
                        self._aa_highlight = new_focus
                        self._aa_highlight_seq = feat.aa_seq
                        self._highlight = None
                        self.show_transcript(self.transcript)
                    return
            self._clear_highlights()

        else:
            # Clicked on DNA/blank/header — clear focus, return to default coloring
            self._clear_highlights()

    @on(events.Click, "#seq-cds-btn")
    def _on_cds_click(self, event: events.Click) -> None:
        event.stop()
        if not self._cds_dna:
            return
        cds_area = self.query_one("#seq-cds-area", TextArea)
        if cds_area.has_class("visible"):
            # Toggle off
            cds_area.remove_class("visible")
        else:
            # Show CDS DNA in selectable TextArea and copy to clipboard
            cds_area.load_text(self._cds_dna)
            cds_area.add_class("visible")
            cds_area.select_all()
            cds_area.focus()
            self.app.copy_to_clipboard(self._cds_dna)
            self.app.notify(f"CDS DNA copied ({len(self._cds_dna)} bp)", severity="information")


# ══════════════════════════════════════════════════════════════════════════════
# Statistics panel widget
# ══════════════════════════════════════════════════════════════════════════════

_BUCKET_LABELS = ["<200 bp", "200–500 bp", "500–1k bp", "1k–2k bp", "2k–5k bp", ">5k bp"]

_FILTER_RE = re.compile(r"(len|gc)\s*([<>]=?)\s*([\d.]+)", re.IGNORECASE)


def _filter_transcripts(
    transcripts: list[Transcript], query: str, bookmarks: set[str] | None = None,
) -> list[Transcript]:
    """Filter transcripts by text + optional len/gc numeric predicates.

    Example: 'ribosom len>500 len<2000 gc>40'
    Special keyword: 'bookmarked' to show only bookmarked transcripts.
    """
    text_parts = []
    predicates: list[tuple[str, str, float]] = []
    only_bookmarked = False
    for token in query.split():
        if token.lower() == "bookmarked":
            only_bookmarked = True
            continue
        m = _FILTER_RE.fullmatch(token)
        if m:
            try:
                predicates.append((m.group(1).lower(), m.group(2), float(m.group(3))))
            except ValueError:
                # Malformed number like "1.2.3" — treat as plain text instead
                text_parts.append(token.lower())
        else:
            text_parts.append(token.lower())
    text_query = " ".join(text_parts)

    results = transcripts
    if only_bookmarked and bookmarks:
        results = [t for t in results if t.id in bookmarks]
    if text_query:
        results = [
            t for t in results
            if text_query in t.id.lower() or text_query in t.description.lower()
        ]
    for field, op, val in predicates:
        def _check(t: Transcript, f=field, o=op, v=val) -> bool:
            actual = float(t.length) if f == "len" else t.gc_content
            if o == ">": return actual > v
            if o == ">=": return actual >= v
            if o == "<": return actual < v
            if o == "<=": return actual <= v
            return True
        results = [t for t in results if _check(t)]
    return results


def _compute_basic_stats(transcripts: list[Transcript]) -> dict:
    """Fast, ORF-free stats. Suitable for immediate display after load."""
    lengths = sorted(t.length for t in transcripts)
    n = len(lengths)
    if n == 0:
        return {
            "n": 0, "total_bases": 0,
            "shortest": 0, "longest": 0,
            "mean_len": 0, "median_len": 0,
            "n50": 0, "mean_gc": 0,
            "bucket_counts": [0] * 6,
            "orf_count": 0, "orf_lengths": [],
            "orfs_pending": True,
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
        "orf_count": 0, "orf_lengths": [],
        "orfs_pending": True,
    }


def _compute_orf_stats(transcripts: list[Transcript]) -> dict:
    """ORF-only stats — the expensive pass. Merge into a basic-stats dict."""
    orf_count = 0
    orf_lengths: list[int] = []
    for t in transcripts:
        aa = _longest_orf_aa_length(t.sequence)
        if aa:
            orf_count += 1
            orf_lengths.append(aa)
    return {
        "orf_count": orf_count,
        "orf_lengths": sorted(orf_lengths),
        "orfs_pending": False,
    }


def _compute_stats(transcripts: list[Transcript]) -> dict:
    """Full stats (basic + ORF). Kept for callers that want a single blob."""
    stats = _compute_basic_stats(transcripts)
    if stats["n"] == 0:
        return stats
    stats.update(_compute_orf_stats(transcripts))
    return stats


class StatsPanel(ScrollableContainer):
    DEFAULT_CSS = """
    StatsPanel { height: 1fr; padding: 1 2; }
    StatsPanel Static { height: auto; }
    StatsPanel #stats-buttons { height: 3; margin: 0 0 1 0; }
    StatsPanel #stats-buttons Button { margin-right: 1; }
    """

    transcript: reactive[Transcript | None] = reactive(None)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._global_stats: dict | None = None
        self._fasta_path: str = ""
        # Cached Rich renderables for the non-changing parts of the panel.
        # Rebuilt only when global stats change (load), not when the
        # selected transcript changes (hot path for arrow-key scrolling).
        self._global_renderables: list | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="stats-buttons"):
            yield Button("Compute Statistics", id="stats-compute", variant="primary")
            yield Button("Export Stats CSV", id="stats-export", variant="default", disabled=True)
        yield Static("No transcriptome loaded.", id="stats-content")

    _last_shown_id: str = ""

    def on_mount(self) -> None:
        self._update_display()

    def reset_for_new_dataset(self) -> None:
        """Called after a new FASTA/project load — clear stale stats and
        re-enable the Compute button so the user can trigger a fresh run."""
        self._global_stats = None
        self._global_renderables = None
        self._last_shown_id = ""
        try:
            self.query_one("#stats-compute", Button).disabled = False
            self.query_one("#stats-compute", Button).label = "Compute Statistics"
            self.query_one("#stats-export", Button).disabled = True
        except Exception:
            pass
        self._update_display()

    @on(Button.Pressed, "#stats-compute")
    def _on_compute_pressed(self) -> None:
        """Kick off the two-phase stats computation on the main app."""
        transcripts = getattr(self.app, "_transcripts", None)
        if not transcripts:
            self._update_display_message("[yellow]Load a transcriptome first.[/]")
            return
        path = getattr(self.app, "_fasta_path", "") or ""
        btn = self.query_one("#stats-compute", Button)
        btn.label = "Computing…"
        btn.disabled = True
        self.app._compute_stats_bg(transcripts, path)

    def _update_display_message(self, message: str) -> None:
        """Replace the panel body with a one-line status message."""
        try:
            self.query_one("#stats-content", Static).update(message)
        except Exception:
            pass

    def watch_transcript(self, t: Transcript | None) -> None:
        new_id = t.id if t is not None else ""
        if new_id == self._last_shown_id:
            return
        self._last_shown_id = new_id
        self._update_display()

    def render_stats(self, s: dict, fasta_path: str) -> None:
        self._global_stats = s
        self._fasta_path = fasta_path
        self._global_renderables = None  # invalidate cache — rebuild on next display
        # Update button states: export enabled once we have stats; compute
        # button changes label based on whether phase 2 (ORF) is still pending.
        try:
            self.query_one("#stats-export", Button).disabled = False
            compute_btn = self.query_one("#stats-compute", Button)
            if s.get("orfs_pending"):
                compute_btn.label = "Computing ORFs…"
                compute_btn.disabled = True
            else:
                compute_btn.label = "Recompute"
                compute_btn.disabled = False
        except Exception:
            pass
        self._update_display()

    def _build_global_renderables(self) -> list:
        """Expensive: build all the global-stats Rich tables. Cached."""
        s = self._global_stats
        assert s is not None
        n = s["n"]
        elements: list = []

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
            dist.add_row(label, f"{count:,}", f"{count / n * 100:.1f}%" if n else "0.0%")
        elements.append(dist)

        orf_count = s.get("orf_count", 0)
        orf_lengths = s.get("orf_lengths", [])
        orfs_pending = s.get("orfs_pending", False)
        elements.append(Text(""))
        if orfs_pending:
            elements.append(Text(
                "  ORF Statistics: scanning 6 frames…",
                style="dim italic",
            ))
        elif orf_count > 0 and orf_lengths:
            orf_grid = Table.grid(padding=(0, 3))
            orf_grid.add_column(style="bold cyan", no_wrap=True)
            orf_grid.add_column(no_wrap=True)
            orf_grid.title = "ORF Statistics (longest M-initiated ORF per transcript, ≥30 aa)"
            orf_grid.title_style = "bold magenta"
            orf_grid.add_row("Transcripts with ORF", f"{orf_count:,} / {n:,} ({orf_count / n * 100:.1f}%)")
            orf_grid.add_row("Shortest ORF", f"{orf_lengths[0]:,} aa")
            orf_grid.add_row("Longest ORF", f"{orf_lengths[-1]:,} aa")
            mean_orf = sum(orf_lengths) / len(orf_lengths)
            median_orf = orf_lengths[len(orf_lengths) // 2]
            orf_grid.add_row("Mean ORF length", f"{mean_orf:,.1f} aa")
            orf_grid.add_row("Median ORF length", f"{median_orf:,} aa")
            elements.append(orf_grid)

        return elements

    def _update_display(self) -> None:
        try:
            content = self.query_one("#stats-content", Static)
        except Exception:
            return

        if not self._global_stats:
            # No stats computed yet — show a helpful prompt that reflects
            # whether a transcriptome is loaded at all.
            transcripts = getattr(self.app, "_transcripts", None)
            if not transcripts:
                content.update("No transcriptome loaded. Open a FASTA file first.")
                return
            fasta_path = getattr(self.app, "_fasta_path", "") or "(unknown)"
            n = len(transcripts)
            content.update(
                f"[bold]{n:,}[/] transcripts loaded from [cyan]{fasta_path}[/]\n\n"
                f"Press [bold]Compute Statistics[/] to scan length, GC, and ORF "
                f"distributions. The basic stats appear immediately; the ORF "
                f"scan runs as a second phase."
            )
            return

        # Cheap: rebuild only the "Selected Transcript" grid each call.
        elements: list = []
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

        # Reuse cached global renderables.
        if self._global_renderables is None:
            self._global_renderables = self._build_global_renderables()
        elements.extend(self._global_renderables)

        content.update(Group(*elements))

    @on(Button.Pressed, "#stats-export")
    def _export_stats(self) -> None:
        if not self._global_stats:
            return
        s = self._global_stats
        path = str(Path.home() / "transcriptome_stats.csv")
        headers = ["Metric", "Value"]
        rows = [
            ["File", self._fasta_path],
            ["Total transcripts", str(s["n"])],
            ["Total bases", str(s["total_bases"])],
            ["Shortest", str(s["shortest"])],
            ["Longest", str(s["longest"])],
            ["Mean length", f"{s['mean_len']:.1f}"],
            ["Median length", str(s["median_len"])],
            ["N50", str(s["n50"])],
            ["Mean GC%", f"{s['mean_gc']:.1f}"],
        ]
        for label, count in zip(_BUCKET_LABELS, s["bucket_counts"]):
            rows.append([f"Bucket {label}", str(count)])
        _export_csv(path, headers, rows)
        self.app.query_one("#app-status", Static).update(f"[green]Exported stats to {path}[/]")


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


def _export_csv(path: str, headers: list[str], rows: list[list[str]]) -> None:
    """Write rows to a CSV file."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)


class HelpModal(ModalScreen[None]):
    """Help screen showing keyboard shortcuts and features."""
    DEFAULT_CSS = """
    HelpModal { align: center middle; }
    HelpModal #help-dialog {
        width: 72; height: auto; max-height: 80%;
        border: thick $primary 80%; background: $surface; padding: 1 2;
    }
    HelpModal #help-title { text-align: center; text-style: bold; }
    HelpModal #help-body { height: auto; padding: 1 0; }
    HelpModal Button { margin: 1 0 0 0; width: 100%; }
    """
    BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Label("ScriptoScope Help", id="help-title")
            yield Static(
                "[bold cyan]Keyboard Shortcuts[/]\n"
                "  [bold]Ctrl+O[/]  Open FASTA file\n"
                "  [bold]Ctrl+S[/]  Save project\n"
                "  [bold]Ctrl+G[/]  Search GenBank (TSA)\n"
                "  [bold]Ctrl+B[/]  Focus BLAST tab\n"
                "  [bold]Ctrl+P[/]  Focus Pfam / HMM tab\n"
                "  [bold]Ctrl+F[/]  Focus filter input\n"
                "  [bold]Ctrl+C[/]  Copy CDS / highlighted region\n"
                "  [bold]Ctrl+R[/]  Copy reverse complement\n"
                "  [bold]Ctrl+D[/]  Toggle bookmark on selected transcript\n"
                "  [bold]?[/]       Show this help\n\n"
                "[bold cyan]Filter Syntax[/]\n"
                "  Type text to search by ID or description.\n"
                "  Use [bold]len>500[/], [bold]len<2000[/], [bold]gc>40[/], [bold]gc<60[/]\n"
                "  to filter by length or GC%. Combine freely:\n"
                "  [dim]ribosom len>500 gc>45[/]\n\n"
                "[bold cyan]Column Sorting[/]\n"
                "  Click column headers (ID, Length, GC%) to sort.\n"
                "  Click again to reverse.\n\n"
                "[bold cyan]Tabs[/]\n"
                "  [bold]Sequence[/]  — DNA viewer with CDS, Pfam annotations, go-to-position\n"
                "  [bold]BLAST[/]     — Local BLAST+ and NCBI BlastP search\n"
                "  [bold]Pfam/HMM[/]  — pyhmmer domain scanning with Pfam-A\n"
                "  [bold]Statistics[/] — Transcriptome summary stats\n\n"
                "[bold cyan]Export[/]\n"
                "  Use Export CSV buttons in BLAST, Pfam, and Statistics tabs.\n"
                "  Use Ctrl+E to export bookmarked transcripts as FASTA.\n",
                id="help-body",
            )
            yield Button("Close", id="help-close", variant="primary")

    @on(Button.Pressed, "#help-close")
    def _close(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """Simple yes/no confirmation dialog."""
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal #confirm-dialog {
        width: 50; height: 7;
        border: thick $primary 80%; background: $surface; padding: 1 2;
    }
    ConfirmModal #confirm-msg { height: 1; text-align: center; }
    ConfirmModal #confirm-buttons { height: 3; align: center middle; }
    ConfirmModal Button { margin: 0 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message: str = "Are you sure?") -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", id="confirm-yes", variant="error")
                yield Button("No", id="confirm-no", variant="primary")

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no(self, event: Button.Pressed) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SaveProjectModal(ModalScreen[str | None]):
    """Simple modal to confirm save path."""
    DEFAULT_CSS = """
    SaveProjectModal { align: center middle; }
    SaveProjectModal #save-dialog {
        width: 80; height: auto;
        border: thick $primary 80%; background: $surface; padding: 1 2;
    }
    SaveProjectModal #save-title {
        height: 1; text-style: bold; text-align: center;
    }
    SaveProjectModal #save-path { margin: 1 0; }
    SaveProjectModal #save-status { height: 1; text-align: center; margin: 0 0; }
    SaveProjectModal #save-buttons { height: 3; align: right middle; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_path: str = "") -> None:
        super().__init__()
        self._default = default_path
        self._saving = False

    def compose(self) -> ComposeResult:
        with Vertical(id="save-dialog"):
            yield Label("Save Project", id="save-title")
            yield Input(self._default, id="save-path", placeholder="Path to save project…")
            yield Static("", id="save-status")
            with Horizontal(id="save-buttons"):
                yield Button("Save", id="save-ok", variant="primary")
                yield Button("Cancel", id="save-cancel")

    @on(Button.Pressed, "#save-ok")
    def _save(self, event: Button.Pressed) -> None:
        if self._saving:
            return
        path = self.query_one("#save-path", Input).value.strip()
        if not path:
            return
        self._saving = True
        self.query_one("#save-ok", Button).disabled = True
        self.query_one("#save-path", Input).disabled = True
        self.query_one("#save-status", Static).update("[dim]Saving…[/]")
        self._run_save(path)

    @work(exclusive=True, thread=True, group="modal-save")
    def _run_save(self, path: str) -> None:
        try:
            app = self.app
            scan_cache = {}
            confirm_cache = {}
            try:
                hmmer = app.query_one("#hmmer-panel")
                scan_cache = getattr(hmmer, "_scan_cache", {})
                confirm_cache = getattr(hmmer, "_confirm_cache", {})
            except Exception:
                pass
            save_project(
                path, app._transcripts, app._fasta_path,
                scan_cache=scan_cache,
                confirm_cache=confirm_cache,
                pfam_hits=app._pfam_hits,
            )

            def _on_saved() -> None:
                self.query_one("#save-status", Static).update(
                    f"[bold green]Saved to {Path(path).name}[/]"
                )
                app._set_status(f"[green]Project saved: {path}[/]")
                app._refresh_transcriptome_select()
                self.set_timer(1.2, lambda: self.dismiss(path))

            self.app.call_from_thread(_on_saved)
        except Exception as exc:
            _log.exception("Save project failed: %s", exc)

            def _on_error() -> None:
                self.query_one("#save-status", Static).update(
                    f"[bold red]Save failed: {exc}[/]"
                )
                self._saving = False
                self.query_one("#save-ok", Button).disabled = False
                self.query_one("#save-path", Input).disabled = False

            self.app.call_from_thread(_on_error)

    @on(Button.Pressed, "#save-cancel")
    def _cancel_btn(self, event: Button.Pressed) -> None:
        if not self._saving:
            self.dismiss(None)

    def action_cancel(self) -> None:
        if not self._saving:
            self.dismiss(None)


class GenBankSearchModal(ModalScreen[tuple[str, str] | None]):
    """Modal for searching and downloading GenBank transcriptomes."""
    DEFAULT_CSS = """
    GenBankSearchModal { align: center middle; }
    GenBankSearchModal #gb-dialog {
        width: 100; height: 30;
        border: thick $primary 80%; background: $surface;
    }
    GenBankSearchModal #gb-title {
        height: 1; background: $primary; color: $text;
        text-align: center; text-style: bold; padding: 0 1;
    }
    GenBankSearchModal #gb-search-row {
        height: 3; padding: 0 1; align: left middle;
    }
    GenBankSearchModal #gb-search-label { width: 10; content-align: right middle; }
    GenBankSearchModal #gb-search-input { width: 1fr; }
    GenBankSearchModal #gb-table { height: 1fr; margin: 0 1; }
    GenBankSearchModal #gb-status {
        height: 1; padding: 0 1; color: $text-muted;
    }
    GenBankSearchModal #gb-buttons {
        height: 3; padding: 0 1; align: right middle;
    }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[GenBankResult] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="gb-dialog"):
            yield Label(" GenBank Transcriptome Search", id="gb-title")
            with Horizontal(id="gb-search-row"):
                yield Label("Organism:", id="gb-search-label")
                yield Input(placeholder="e.g. Epipremnum aureum", id="gb-search-input")
                yield Button("Search", id="gb-search-btn", variant="primary")
            yield DataTable(id="gb-table", zebra_stripes=True, cursor_type="row")
            yield Static("Enter a species or genus name and press Search.", id="gb-status")
            with Horizontal(id="gb-buttons"):
                yield Button("Download Selected", id="gb-download-btn", variant="success")
                yield Button("Cancel", id="gb-cancel-btn")

    def on_mount(self) -> None:
        table = self.query_one("#gb-table", DataTable)
        table.add_columns("Accession", "Organism", "Title", "Length", "Updated")

    @on(Button.Pressed, "#gb-search-btn")
    def _do_search(self, event: Button.Pressed) -> None:
        query = self.query_one("#gb-search-input", Input).value.strip()
        if not query:
            self.query_one("#gb-status", Static).update("[yellow]Enter an organism name.[/]")
            return
        if not _entrez_available():
            self.query_one("#gb-status", Static).update(
                "[red]BioPython Entrez not available — pip install biopython[/]"
            )
            return
        self.query_one("#gb-status", Static).update(f"Searching NCBI for '{query}'…")
        self._run_search(query)

    @work(exclusive=True, thread=True, group="gb-search")
    def _run_search(self, query: str) -> None:
        try:
            results = genbank_search_transcriptomes(query, max_results=10)
            def _apply() -> None:
                self._results = results
                table = self.query_one("#gb-table", DataTable)
                table.clear()
                if not results:
                    self.query_one("#gb-status", Static).update(
                        "[yellow]No TSA transcriptome results found.[/]"
                    )
                    return
                for r in results:
                    table.add_row(
                        r.accession,
                        r.organism[:30],
                        r.title[:40],
                        f"{r.seq_count:,}" if r.seq_count else "–",
                        r.update_date,
                        key=r.accession,
                    )
                self.query_one("#gb-status", Static).update(
                    f"[green]{len(results)} results. Select one and click Download.[/]"
                )
            self.app.call_from_thread(_apply)
        except Exception as exc:
            self.app.call_from_thread(
                self.query_one("#gb-status", Static).update,
                f"[red]Search failed: {exc}[/]",
            )

    @on(Button.Pressed, "#gb-download-btn")
    def _do_download(self, event: Button.Pressed) -> None:
        table = self.query_one("#gb-table", DataTable)
        if not self._results or table.cursor_row is None:
            self.query_one("#gb-status", Static).update("[yellow]Select a result first.[/]")
            return
        idx = table.cursor_row
        if idx < 0 or idx >= len(self._results):
            return
        result = self._results[idx]
        self.query_one("#gb-status", Static).update(
            f"Downloading {result.accession}…"
        )
        self._run_download(result)

    @work(exclusive=True, thread=True, group="gb-download")
    def _run_download(self, result: GenBankResult) -> None:
        try:
            import asyncio
            dl_dir = Path.home() / ".scriptoscope" / "downloads"

            def _progress(msg: str) -> None:
                self.app.call_from_thread(
                    self.query_one("#gb-status", Static).update, msg,
                )

            fasta_path = asyncio.run(
                genbank_download_fasta(result.accession, dl_dir, progress_cb=_progress)
            )
            # Save metadata for dropdown label
            meta_path = Path(fasta_path).with_suffix(".meta.json")
            meta_path.write_text(json.dumps({
                "accession": result.accession,
                "organism": result.organism,
                "title": result.title,
            }), encoding="utf-8")
            def _done() -> None:
                self.dismiss((result.accession, fasta_path))
            self.app.call_from_thread(_done)
        except Exception as exc:
            self.app.call_from_thread(
                self.query_one("#gb-status", Static).update,
                f"[red]Download failed: {exc}[/]",
            )

    @on(Button.Pressed, "#gb-cancel-btn")
    def _cancel_btn(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
            # Recognize FASTA with optional .gz suffix (e.g. foo.fasta.gz).
            lname = f.name.lower()
            if lname.endswith(".gz"):
                lname = lname[:-3]
            is_fasta = Path(lname).suffix in _FASTA_EXTENSIONS
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


_DNA_BASE_CHARS = frozenset("ACGTUNRYSWKMBDHVacgtunryswkmbdhv")
_IGNORED_SEQ_CHARS = frozenset(" \t\n\r0123456789>")


def _detect_seq_type(seq: str) -> str:
    """Return 'dna' if the sequence looks like nucleotides, else 'protein'.

    Samples the first 1,000 alphabetic characters — sufficient to distinguish
    DNA from protein without scanning multi-megabase inputs.
    """
    seen_alpha = False
    count = 0
    for ch in seq:
        if ch in _IGNORED_SEQ_CHARS:
            continue
        if ch not in _DNA_BASE_CHARS:
            return "protein"
        seen_alpha = True
        count += 1
        if count >= 1000:
            break
    return "dna" if seen_alpha else "protein"


_CLEAN_SEQ_RE = re.compile(r"[^A-Za-z*]+")


def _clean_seq(raw: str) -> str:
    """Strip FASTA headers, whitespace, and digits from pasted sequence text."""
    parts: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        cleaned = _CLEAN_SEQ_RE.sub("", stripped)
        if cleaned:
            parts.append(cleaned)
    return "".join(parts)


class SelectableTextArea(TextArea):
    """TextArea that supports Ctrl+A to select all."""

    def _on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+a":
            event.prevent_default()
            event.stop()
            self.select_all()
            return
        super()._on_key(event)


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
    _row_to_subject: dict[str, str] = {}  # row_key -> original subject_id
    _row_to_hit: dict[str, BlastHit] = {}  # row_key -> full BlastHit

    def compose(self) -> ComposeResult:
        with Vertical(classes="blast-form"):
            with Horizontal(classes="blast-row"):
                yield Label("Query:", classes="blast-label")
                with RadioSet(id="blast-query-source"):
                    yield RadioButton("Custom sequence", value=True, id="blast-src-custom")
                    yield RadioButton("Selected transcript", id="blast-src-transcript")
            with Vertical(id="blast-query-area"):
                yield SelectableTextArea(
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
                yield Button("NCBI BlastP", id="blast-ncbi", variant="default")
                yield Button("Build BLAST DB", id="blast-build-db", variant="warning")
                yield Button("Export CSV", id="blast-export", variant="default")
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
        # Custom sequence is the default — show query area
        self.query_one("#blast-query-area").display = True

    def set_transcriptome_db(self, db_path: str) -> None:
        self.query_one("#blast-db-input", Input).value = db_path

    @on(RadioSet.Changed, "#blast-query-source")
    def _query_source_changed(self, event: RadioSet.Changed) -> None:
        use_custom = event.index == 0
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

    @on(Button.Pressed, "#blast-build-db")
    def _build_blast_db(self) -> None:
        fasta = getattr(self.app, "_fasta_path", "")
        if not fasta:
            self._set_status("[yellow]Load a transcriptome first.[/]")
            return
        if not blast_available():
            self._set_status("[red]BLAST+ not installed.[/]")
            return
        btn = self.query_one("#blast-build-db", Button)
        if btn.has_class("building"):
            return
        btn.add_class("building")
        btn.label = "Building…"
        btn.variant = "success"
        self.app.action_build_blast_db(interactive=False)

    @on(Button.Pressed, "#blast-export")
    def _export_blast(self) -> None:
        table = self.query_one("#blast-table", DataTable)
        if table.row_count == 0:
            self._set_status("[yellow]No BLAST results to export.[/]")
            return
        path = str(Path.home() / "blast_results.csv")
        headers = ["Subject", "% ID", "Aln Len", "E-value", "Bit Score",
                    "Q Start", "Q End", "S Start", "S End"]
        rows = []
        for row_key in table.rows:
            row = table.get_row(row_key)
            rows.append([str(c) for c in row])
        _export_csv(path, headers, rows)
        self._set_status(f"[green]Exported {len(rows)} hits to {path}[/]")

    @on(Button.Pressed, "#blast-ncbi")
    def _run_ncbi_blastp(self) -> None:
        btn = self.query_one("#blast-ncbi", Button)

        # If already running, prompt to cancel
        if btn.has_class("running-ncbi"):
            self.app.push_screen(
                ConfirmModal("Stop NCBI BlastP search?"),
                self._on_ncbi_cancel_confirm,
            )
            return

        query = self._get_query()
        if query is None:
            return
        query_id, query_seq = query

        # Get protein sequence: translate if DNA, or use directly if protein
        seq_type = _detect_seq_type(query_seq)
        if seq_type == "dna":
            orf = _find_longest_orf(query_seq, query_id)
            if orf and orf.sequence:
                protein_seq = orf.sequence
            else:
                self._set_status("[yellow]No ORF found — paste a protein sequence or use a transcript with an ORF.[/]")
                return
        else:
            protein_seq = query_seq

        _ncbi_blast_cancel.clear()
        btn.add_class("running-ncbi")
        btn.label = "Cancel Search"
        btn.variant = "error"
        self._ncbi_blastp_worker(protein_seq)

    def _on_ncbi_cancel_confirm(self, confirmed: bool) -> None:
        if confirmed:
            _ncbi_blast_cancel.set()
            self._set_status("[yellow]Cancelling NCBI BlastP…[/]")

    @work(exclusive=True, thread=True, group="ncbi-blast")
    def _ncbi_blastp_worker(self, protein_seq: str) -> None:
        try:
            def _progress(msg: str) -> None:
                self.app.call_from_thread(self._set_status, msg)

            hits = ncbi_blastp(protein_seq, max_hits=10, progress_cb=_progress)

            def _apply() -> None:
                table = self.query_one("#blast-table", DataTable)
                table.clear()
                self._row_to_subject = {}
                for i, h in enumerate(hits):
                    row_key = f"ncbi_{h.subject_id}_{i}"
                    self._row_to_subject[row_key] = h.subject_id
                    self._row_to_hit[row_key] = h
                    table.add_row(
                        h.subject_id[:60], f"{h.pct_identity:.1f}",
                        str(h.alignment_length), f"{h.evalue:.2e}", f"{h.bit_score:.1f}",
                        str(h.query_start), str(h.query_end),
                        str(h.subject_start), str(h.subject_end),
                        key=row_key,
                    )
                self._set_status(f"[green]{len(hits)} NCBI BlastP hits found.[/]")
                btn = self.query_one("#blast-ncbi", Button)
                btn.remove_class("running-ncbi")
                btn.label = "NCBI BlastP"
                btn.variant = "default"

            self.app.call_from_thread(_apply)
        except NCBIBlastCancelled:
            def _cancelled() -> None:
                self._set_status("[yellow]NCBI BlastP search cancelled.[/]")
                btn = self.query_one("#blast-ncbi", Button)
                btn.remove_class("running-ncbi")
                btn.label = "NCBI BlastP"
                btn.variant = "default"
            self.app.call_from_thread(_cancelled)
        except Exception as exc:
            def _err() -> None:
                self._set_status(f"[red]NCBI BlastP error: {exc}[/]")
                btn = self.query_one("#blast-ncbi", Button)
                btn.remove_class("running-ncbi")
                btn.label = "NCBI BlastP"
                btn.variant = "default"
            self.app.call_from_thread(_err)
            _log.exception("NCBI BlastP failed: %s", exc)

    def _get_query(self) -> tuple[str, str] | None:
        """Return (query_id, query_seq) or None on error."""
        radio = self.query_one("#blast-query-source", RadioSet)
        use_custom = radio.pressed_index == 0

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
        # BLAST DBs are multi-file prefixes — check for any index file.
        db_base = Path(db).expanduser()
        if not any(db_base.parent.glob(db_base.name + ".*")):
            self._set_status(f"[red]BLAST database not found: {db}[/]")
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
        self._row_to_subject = {}
        try:
            hits, _ = await local_blast(
                query_seq=query_seq, query_id=query_id, db_path=db,
                program=program, evalue=evalue, max_hits=maxhits,
            )
            for i, h in enumerate(hits):
                row_key = f"{h.subject_id}_{i}"
                self._row_to_subject[row_key] = h.subject_id
                self._row_to_hit[row_key] = h
                table.add_row(
                    h.subject_id[:40], f"{h.pct_identity:.1f}",
                    str(h.alignment_length), f"{h.evalue:.2e}", f"{h.bit_score:.1f}",
                    str(h.query_start), str(h.query_end),
                    str(h.subject_start), str(h.subject_end),
                    key=row_key,
                )
            self._set_status(f"[green]{len(hits)} hits found.[/]")
        except Exception as exc:
            self._set_status(f"[red]Error: {exc}[/]")
            _log.exception("BLAST worker failed: %s", exc)
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
        self._diagram_cache: dict[tuple, Text] = {}

    def _reset_scan_button(self) -> None:
        """Reset the scan button to its default ready state."""
        try:
            btn = self.query_one("#hmmer-run", Button)
            btn.label = "Scan Selected"
            btn.variant = "primary"
            btn.disabled = False
            btn.remove_class("scanning")
        except Exception:
            pass

    _last_shown_id: str = ""

    def watch_transcript(self, t: Transcript | None) -> None:
        """Show cached results when transcript changes, otherwise clear."""
        # Skip if the transcript id hasn't actually changed — reactive can
        # fire on same-value assignments and we don't want to redo the work.
        new_id = t.id if t is not None else ""
        if new_id == self._last_shown_id:
            return
        self._last_shown_id = new_id
        try:
            table = self.query_one("#hmmer-table", DataTable)
            table.clear()
            diagram = self.query_one("#hmmer-diagram", Static)

            if t is None:
                diagram.update("")
                self._set_status("")
                self._reset_scan_button()
                return

            if t.id in self._scan_cache:
                hits = self._scan_cache[t.id]
                self._display_hits(t, hits, refresh_seq=False)
                conf = self._confirm_cache.get(t.id)
                if conf:
                    tag = "CONFIRMED" if conf.confirmed else "UNCONFIRMED"
                    color = "green" if conf.confirmed else "yellow"
                    self._set_status(f"[green]{len(hits)} Pfam hits (cached).[/] [{color}]CDS {tag}[/{color}]")
                else:
                    self._set_status(f"[green]{len(hits)} domain hits (cached).[/]")
                # Already scanned — show as complete
                btn = self.query_one("#hmmer-run", Button)
                btn.label = "Scan Complete"
                btn.disabled = True
                btn.variant = "default"
                btn.remove_class("scanning")
            else:
                diagram.update("")
                self._set_status("")
                self._reset_scan_button()
        except Exception:
            pass

    def _display_hits(
        self, t: Transcript, hits: list[HmmerHit], *, refresh_seq: bool = True,
    ) -> None:
        """Populate table and diagram from a list of hits.

        Args:
            refresh_seq: If True, also refresh the Sequence tab annotations.
                Set to False when called from watch_transcript (the
                SequenceViewer already handles its own refresh).
        """
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
            diag_w = max(w, 60)
            cache_key = (t.id, diag_w, id(conf) if conf else None, len(hits))
            diagram = self._diagram_cache.get(cache_key)
            if diagram is None:
                diagram = render_orf_diagram(t, hits, width=diag_w, confirmation=conf)
                self._diagram_cache[cache_key] = diagram
            self.query_one("#hmmer-diagram", Static).update(diagram)
        except Exception as exc:
            _log.exception("Diagram render failed: %s", exc)
        # Refresh Sequence tab so annotations appear on the DNA view
        if refresh_seq:
            try:
                sv = self.app.query_one("#seq-viewer")
                if hasattr(sv, "refresh_annotations"):
                    _log.info("_display_hits: calling refresh_annotations for %s", t.id)
                    sv.refresh_annotations()
            except Exception as exc:
                _log.exception("_display_hits: refresh_annotations failed: %s", exc)

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
                yield Button("Export CSV", id="hmmer-export", variant="default")
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

    @on(Button.Pressed, "#hmmer-export")
    def _export_hmmer(self) -> None:
        table = self.query_one("#hmmer-table", DataTable)
        if table.row_count == 0:
            self._set_status("[yellow]No HMMER results to export.[/]")
            return
        path = str(Path.home() / "hmmer_results.csv")
        headers = ["Family", "Accession", "E-value", "Score", "Bias",
                    "HMM From", "HMM To", "Ali From", "Ali To", "Description"]
        rows = []
        for row_key in table.rows:
            row = table.get_row(row_key)
            rows.append([str(c) for c in row])
        _export_csv(path, headers, rows)
        self._set_status(f"[green]Exported {len(rows)} hits to {path}[/]")

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
        btn = self.query_one("#hmmer-run", Button)
        # Ignore presses while scanning
        if btn.has_class("scanning"):
            return
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
            btn.label = "Scan Complete"
            btn.disabled = True
            return
        db = self.query_one("#hmmer-db-input", Input).value.strip()
        if not db:
            self._set_status("[yellow]Enter an HMM database path.[/]")
            return
        if not Path(db).expanduser().is_file():
            self._set_status(f"[red]HMM database not found: {db}[/]")
            return
        evalue_str = self.query_one("#hmmer-evalue", Input).value.strip()
        translate = self.query_one("#hmmer-translate", Switch).value
        try:
            evalue = float(evalue_str)
        except ValueError:
            self._set_status("[red]Invalid e-value.[/]")
            return
        # Enter scanning state
        btn.label = "Scanning..."
        btn.variant = "success"
        btn.add_class("scanning")
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
            # Disable button after successful scan
            btn = self.query_one("#hmmer-run", Button)
            btn.label = "Scan Complete"
            btn.disabled = True
            btn.variant = "default"
            btn.remove_class("scanning")
        except HMMCancelled:
            self._set_status("[dim]Scan cancelled.[/]")
            self._reset_scan_button()
        except Exception as exc:
            self._set_status(f"[red]Error: {exc}[/]")
            self._reset_scan_button()
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
                self._update_sequence_tab(t, hits)
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
        if not Path(db).expanduser().is_file():
            self._set_status(f"[red]HMM database not found: {db}[/]")
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
            status = (
                f"Scanning collection… {current:,}/{total:,} HMMs ({pct}%) "
                f"— elapsed {int(elapsed // 60)}m {int(elapsed % 60)}s, ETA {eta_str}"
            )

            def _update_ui() -> None:
                self._show_progress(current, total)
                self._set_status(status)

            self.app.call_from_thread(_update_ui)

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

    def _update_sequence_tab(self, t: Transcript, hits: list[HmmerHit]) -> None:
        """Directly update the Sequence tab body with CDS/Pfam annotations."""
        try:
            body = self.app.query_one("#seq-body", Static)
            orf = _find_longest_orf(t.sequence, t.id)
            top_hits = sorted(hits, key=lambda h: h.evalue)[:5]
            if orf:
                body.update(colorize_sequence_annotated(t.sequence, orf=orf, hits=top_hits).text)
                _log.info("Updated Sequence tab with annotations for %s", t.id)
            else:
                _log.info("No ORF found for %s, skipping sequence annotation", t.id)
        except Exception as exc:
            _log.exception("_update_sequence_tab failed: %s", exc)

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
    #sidebar-filters Input { margin-bottom: 0; }
    #transcriptome-select { width: 100%; margin-top: 1; }
    #transcript-table { height: 1fr; }
    #content-area { width: 1fr; height: 100%; }
    #app-status {
        height: 1; background: $surface;
        padding: 0 1; color: $text-muted;
        border-top: solid $primary 20%;
    }
    """

    BINDINGS = [
        Binding("ctrl+o", "open_file", "Open"),
        Binding("ctrl+s", "save_project", "Save"),
        Binding("ctrl+g", "genbank_search", "GenBank"),
        Binding("ctrl+b", "focus_blast", "BLAST"),
        Binding("ctrl+p", "focus_pfam", "Pfam Scan"),
        Binding("ctrl+f", "focus_filter", "Filter"),
        Binding("ctrl+c", "copy_cds", "Copy CDS", show=False),
        Binding("ctrl+r", "copy_revcomp", "Copy RevComp", show=False),
        Binding("ctrl+d", "toggle_bookmark", "Bookmark", show=False),
        Binding("ctrl+e", "export_fasta", "Export FASTA", show=False),
        Binding("question_mark", "show_help", "Help"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def action_quit(self) -> None:
        _hmm_cancel.set()
        _restore_terminal()
        os._exit(0)

    def action_copy_cds(self) -> None:
        """Copy highlighted AA, highlighted DNA, or CDS DNA to clipboard (Ctrl+C)."""
        try:
            viewer = self.query_one("#seq-viewer", SequenceViewer)
            if viewer._aa_highlight_seq:
                self.copy_to_clipboard(viewer._aa_highlight_seq)
                self.notify(
                    f"AA copied ({len(viewer._aa_highlight_seq)} aa, N→C)",
                    severity="information",
                )
            elif viewer._highlight and viewer.transcript:
                # Copy highlighted DNA top strand 5'→3'
                lo, hi = viewer._highlight
                dna = viewer.transcript.sequence[lo:hi]
                self.copy_to_clipboard(dna)
                self.notify(
                    f"DNA copied ({len(dna)} bp, top strand 5'→3')",
                    severity="information",
                )
            else:
                viewer.copy_cds_to_clipboard()
        except Exception:
            pass

    def action_copy_revcomp(self) -> None:
        """Copy reverse complement of highlighted DNA 5'→3' (Ctrl+Shift+C)."""
        try:
            from Bio.Seq import Seq
            viewer = self.query_one("#seq-viewer", SequenceViewer)
            if viewer._highlight and viewer.transcript:
                lo, hi = viewer._highlight
                dna = viewer.transcript.sequence[lo:hi]
                rc = str(Seq(dna).reverse_complement())
                self.copy_to_clipboard(rc)
                self.notify(
                    f"RevComp copied ({len(rc)} bp, 5'→3')",
                    severity="information",
                )
            else:
                self.notify("No DNA highlight active", severity="warning")
        except Exception:
            pass

    def action_toggle_bookmark(self) -> None:
        """Toggle bookmark on the currently selected transcript (Ctrl+D)."""
        try:
            table = self.query_one("#transcript-table", DataTable)
            cursor_row = table.cursor_row
            if cursor_row is None:
                return
            keys = list(table.rows.keys())
            if cursor_row >= len(keys):
                return
            rk = keys[cursor_row]
            tid = str(rk.value)
            t = self._by_id.get(tid)
            if t is None:
                return
            if tid in self._bookmarks:
                self._bookmarks.discard(tid)
                self._set_status(f"Removed bookmark: {tid}")
            else:
                self._bookmarks.add(tid)
                self._set_status(f"[green]Bookmarked: {tid}[/]")
            # Update just the ID cell in place — no full rebuild needed.
            new_label = f"* {t.short_id}" if tid in self._bookmarks else t.short_id
            try:
                table.update_cell(rk, table.ordered_columns[0].key, new_label)
            except Exception:
                # Fall back to full rebuild if the cell API mismatches
                self._populate_table(self._filtered, auto_select=False)
                try:
                    new_idx = next(i for i, k in enumerate(table.rows) if k.value == tid)
                    table.move_cursor(row=new_idx, animate=False)
                except StopIteration:
                    pass
        except Exception:
            pass

    def action_export_fasta(self) -> None:
        """Export bookmarked transcripts as FASTA (Ctrl+E)."""
        if not self._bookmarks:
            self.notify("No bookmarked transcripts. Use Ctrl+D to bookmark.", severity="warning")
            return
        base = Path.home() / "bookmarked_transcripts.fasta"
        # Avoid silently overwriting: add numeric suffix if file exists.
        path_obj = base
        suffix_i = 2
        while path_obj.exists():
            path_obj = base.with_name(f"{base.stem}_{suffix_i}{base.suffix}")
            suffix_i += 1
        path = str(path_obj)
        count = 0
        with open(path, "w", encoding="utf-8") as f:
            chunks: list[str] = []
            for tid in self._bookmarks:
                t = self._by_id.get(tid)
                if t is None:
                    continue
                chunks.append(f">{t.id} {t.description}\n")
                seq = t.sequence
                for i in range(0, len(seq), 80):
                    chunks.append(seq[i:i+80])
                    chunks.append("\n")
                count += 1
            f.write("".join(chunks))
        self._set_status(f"[green]Exported {count} bookmarked transcripts to {path}[/]")

    def action_save_project(self) -> None:
        """Save transcriptome + analysis to a JSON project file (Ctrl+S)."""
        if not self._transcripts:
            self.notify("No transcriptome loaded to save.", severity="warning")
            return
        # Default save path: same dir as FASTA, .scriptoscope.json extension
        if self._fasta_path:
            default_path = str(Path(self._fasta_path).with_suffix(".scriptoscope.json"))
        else:
            default_path = str(Path.home() / "transcriptome.scriptoscope.json")
        # SaveProjectModal handles the actual save internally and dismisses
        # itself when done — no callback needed on this side.
        self.push_screen(SaveProjectModal(default_path=default_path))

    def _load_project_file(self, path: str) -> None:
        """Load a .scriptoscope.json project file."""
        self._do_load_project(path)

    @work(exclusive=True, thread=True, group="load-project")
    def _do_load_project(self, path: str) -> None:
        # Defensive: reject upstream dispatch garbage before it becomes a
        # user-visible error.
        if not path or not isinstance(path, str) or path.startswith("Select."):
            _log.warning("_do_load_project: ignoring invalid path %r", path)
            return
        # Cancel any in-flight work tied to the previous dataset.
        _hmm_cancel.set()
        _ncbi_blast_cancel.set()

        # Pre-flight existence check to produce a clean error instead of a
        # raw "[Errno 2] No such file or directory" toast.
        p = Path(path).expanduser()
        if not p.exists():
            msg = f"Project file not found: {path}"
            self.call_from_thread(self._set_status, f"[red]{msg}[/]")
            self.call_from_thread(
                self.notify,
                f"{msg}\nThe entry may be stale — dropdown will refresh.",
                title="File not found", severity="error", timeout=5,
            )
            self.call_from_thread(self._refresh_transcriptome_select)
            return
        if not p.is_file():
            self.call_from_thread(self._set_status, f"[red]Not a file: {path}[/]")
            return

        self.call_from_thread(self._set_status, f"Loading project {path}…")
        try:
            proj = load_project(path)
        except ProjectFormatError as exc:
            self.call_from_thread(self._set_status, f"[red]Invalid project: {exc}[/]")
            self.call_from_thread(
                self.notify, str(exc), title="Invalid project", severity="error",
            )
            return
        except FileNotFoundError:
            msg = f"Project file disappeared during load: {path}"
            self.call_from_thread(self._set_status, f"[red]{msg}[/]")
            self.call_from_thread(
                self.notify, msg, title="File not found", severity="error",
            )
            return
        except PermissionError as exc:
            self.call_from_thread(self._set_status, f"[red]Permission denied: {exc}[/]")
            self.call_from_thread(
                self.notify, f"Permission denied: {path}",
                title="Load error", severity="error",
            )
            return
        except OSError as exc:
            self.call_from_thread(self._set_status, f"[red]Cannot read project: {exc}[/]")
            return
        except Exception as exc:
            _log.exception("Load project failed: %s", exc)
            self.call_from_thread(self._set_status, f"[red]Error: {exc}[/]")
            return

        transcripts = proj["transcripts"]
        if not transcripts:
            self.call_from_thread(
                self._set_status, "[yellow]Project contains no transcripts.[/]",
            )
            return
        by_id = {t.id: t for t in transcripts}
        visible = transcripts[:_MAX_TABLE_ROWS]
        row_data = [
            (t.short_id, f"{t.length:,}", f"{t.gc_content:.1f}", t.id)
            for t in visible
        ]

        def _apply() -> None:
            self._transcripts = transcripts
            self._filtered = transcripts
            self._by_id = by_id
            self._fasta_path = proj["fasta_path"] or path
            self._pfam_hits = proj.get("pfam_hits", {})
            _longest_orf_cache.clear()
            _hmm_cancel.clear()
            _ncbi_blast_cancel.clear()
            self._populate_table_fast(row_data, len(transcripts), visible)
            self._refresh_transcriptome_select()
            # Restore scan/confirm caches into HmmerPanel, clearing stale diagrams.
            try:
                hmmer = self.query_one("#hmmer-panel")
                hmmer._scan_cache = proj.get("scan_cache", {})
                hmmer._confirm_cache = proj.get("confirm_cache", {})
                hmmer._diagram_cache.clear()
                hmmer._last_shown_id = ""
                self.query_one("#seq-viewer", SequenceViewer)._last_shown_id = ""
                self.query_one("#stats-panel", StatsPanel)._last_shown_id = ""
            except Exception:
                pass
            self._set_status(
                f"[green]{len(transcripts):,} transcripts loaded from project[/]"
            )
            self.notify(
                f"Project loaded: {len(transcripts):,} transcripts",
                title="Project", severity="information", timeout=3,
            )
            # Stats are no longer auto-computed — user triggers via button
            try:
                self.query_one("#stats-panel", StatsPanel).reset_for_new_dataset()
            except Exception:
                pass
        self.call_from_thread(_apply)

    def action_genbank_search(self) -> None:
        """Open GenBank transcriptome search dialog (Ctrl+G)."""
        self.push_screen(GenBankSearchModal(), self._on_genbank_download)

    def _on_genbank_download(self, result: tuple[str, str] | None) -> None:
        """Handle GenBank download result: (accession, fasta_path)."""
        if result:
            acc, fasta_path = result
            self._load_fasta(fasta_path)

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
        self._sort_column: str = ""   # "", "id", "length", "gc"
        self._sort_reverse: bool = False
        self._bookmarks: set[str] = set()  # transcript IDs

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-layout"):
            with Vertical(id="sidebar"):
                with Horizontal(id="sidebar-header"):
                    yield Label("[bold]Transcripts[/]")
                    yield Static("0 loaded", id="transcript-count")
                with Vertical(id="sidebar-filters"):
                    yield Input(placeholder="Filter: text  len>500  gc>40 …", id="filter-input")
                    yield Select(
                        [], prompt="Recent transcriptomes…",
                        id="transcriptome-select", allow_blank=True,
                    )
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
        self._refresh_transcriptome_select()
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

    # ── Transcriptome select dropdown ────────────────────────────────────────

    _refreshing_select: bool = False

    def _discover_transcriptome_files(self) -> list[tuple[str, str]]:
        """Find saved projects and GenBank downloads.

        Returns list of (label, path) tuples sorted by modification time (newest first).
        Only scans ~/.scriptoscope/downloads/ — not home or cwd (too slow).
        """
        entries: list[tuple[float, str, str]] = []  # (mtime, label, path)
        dl_dir = Path.home() / ".scriptoscope" / "downloads"

        # Scan downloads dir for FASTA and project files
        if dl_dir.is_dir():
            for f in dl_dir.iterdir():
                try:
                    if not f.is_file():
                        continue
                    if f.suffix in (".fasta", ".fa", ".fna"):
                        # Check for metadata file with organism name
                        meta_path = f.with_suffix(".meta.json")
                        label = None
                        if meta_path.is_file():
                            try:
                                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                                organism = meta.get("organism", "")
                                if organism:
                                    label = f"\u2913 {organism}"
                            except Exception:
                                pass
                        if not label:
                            label = f"\u2913 {f.stem}"
                        entries.append((f.stat().st_mtime, label, str(f)))
                    elif f.name.endswith(".scriptoscope.json"):
                        label = f"\u2606 {f.stem.replace('.scriptoscope', '')}"
                        entries.append((f.stat().st_mtime, label, str(f)))
                except OSError:
                    continue

        # If a FASTA is currently loaded, include it so it shows as an option
        if self._fasta_path:
            path_str = self._fasta_path
            if not any(e[2] == path_str for e in entries):
                p = Path(path_str)
                try:
                    if p.is_file():
                        entries.append((p.stat().st_mtime, p.name, path_str))
                except OSError:
                    pass

        # Sort newest first, deduplicate by path
        entries.sort(key=lambda e: e[0], reverse=True)
        seen: set[str] = set()
        result: list[tuple[str, str]] = []
        for _, label, path in entries:
            if path not in seen:
                seen.add(path)
                result.append((label, path))
        return result

    def _refresh_transcriptome_select(self) -> None:
        """Refresh the transcriptome dropdown with discovered files."""
        self._refreshing_select = True
        try:
            sel = self.query_one("#transcriptome-select", Select)
            options = self._discover_transcriptome_files()
            sel.set_options(options)
            if self._fasta_path:
                try:
                    sel.value = self._fasta_path
                except Exception:
                    pass
            else:
                sel.clear()
        finally:
            self._refreshing_select = False

    @on(Select.Changed, "#transcriptome-select")
    def _on_transcriptome_select(self, event: Select.Changed) -> None:
        if self._refreshing_select:
            return
        # Reject any non-string value. Real paths are always strings; anything
        # else is a Textual sentinel (Select.BLANK, Select.NULL, None) whose
        # exact identity varies between Textual versions. This catches the
        # reset-to-NULL event fired by set_options() on some versions.
        if not isinstance(event.value, str):
            _log.debug(
                "Ignoring non-string Select value: %r (type=%s)",
                event.value, type(event.value).__name__,
            )
            return
        path = event.value
        _log.info("Transcriptome select changed -> path=%r", path)
        if path == self._fasta_path:
            return
        if path.endswith(".scriptoscope.json"):
            self._load_project_file(path)
        else:
            self._load_fasta(path)  # _parse_fasta auto-detects gzip

    # ── File loading ──────────────────────────────────────────────────────────

    def action_open_file(self) -> None:
        start = self._fasta_path or str(Path.home())
        self.push_screen(FileBrowserModal(start_path=start), self._on_file_selected)

    def _on_file_selected(self, path: str | None) -> None:
        _log.info("FileBrowserModal returned path=%r", path)
        if path:
            if path.endswith(".scriptoscope.json"):
                self._load_project_file(path)
            else:
                self._load_fasta(path)

    @work(exclusive=True, thread=True)
    def _load_fasta(self, path: str) -> None:
        _log.info("_load_fasta invoked with path=%r", path)
        # Defensive: sentinel-looking strings from upstream dispatch bugs
        # should never reach the loader. Silently ignore instead of showing
        # the user a "File not found: Select.NULL" error.
        if not path or not isinstance(path, str) or path.startswith("Select."):
            _log.warning("_load_fasta: ignoring invalid path %r", path)
            return
        # Cancel any in-flight HMM scans from a previous transcriptome so they
        # don't write stale results into the freshly loaded state.
        _hmm_cancel.set()
        _ncbi_blast_cancel.set()

        # Pre-flight existence check — catches stale dropdown entries and
        # dead command-line args before `load_all` turns them into raw
        # "[Errno 2] No such file or directory" noise.
        p = Path(path).expanduser()
        _log.info(
            "_load_fasta pre-flight: resolved=%r exists=%s is_file=%s",
            str(p), p.exists(), p.is_file() if p.exists() else False,
        )
        if not p.exists():
            msg = f"File not found: {path}"
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(self._set_status, f"[red]{msg}[/]")
            self.call_from_thread(
                self.notify,
                f"{msg}\nThe entry may be stale — dropdown will refresh.",
                title="File not found", severity="error", timeout=5,
            )
            # Refresh the dropdown to drop any stale entries.
            self.call_from_thread(self._refresh_transcriptome_select)
            return
        if not p.is_file():
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(
                self._set_status, f"[red]Not a file: {path}[/]",
            )
            self.call_from_thread(
                self.notify, f"Not a regular file: {path}",
                title="Load error", severity="error",
            )
            return

        self.call_from_thread(self._set_status, f"Loading {path}…")
        self.call_from_thread(
            self.notify, f"Loading {Path(path).name}…",
            title="Transcriptome", severity="information", timeout=60,
        )
        try:
            transcripts = load_all(path)
        except FastaFormatError as exc:
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(
                self._set_status, f"[red]Invalid FASTA: {exc}[/]",
            )
            self.call_from_thread(
                self.notify, str(exc), title="Invalid FASTA", severity="error",
            )
            return
        except FileNotFoundError:
            # Race: file existed at pre-flight but vanished before load.
            msg = f"File disappeared during load: {path}"
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(self._set_status, f"[red]{msg}[/]")
            self.call_from_thread(
                self.notify, msg, title="File not found", severity="error",
            )
            return
        except PermissionError as exc:
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(self._set_status, f"[red]Permission denied: {exc}[/]")
            self.call_from_thread(
                self.notify, f"Permission denied: {path}",
                title="Load error", severity="error",
            )
            return
        except Exception as exc:
            _log.exception("Load fasta failed for %s", path)
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(self._set_status, f"[red]Error loading file: {exc}[/]")
            self.call_from_thread(
                self.notify, f"Failed to load: {exc}",
                title="Load error", severity="error",
            )
            return

        if not transcripts:
            self.call_from_thread(self.clear_notifications)
            self.call_from_thread(
                self._set_status, "[yellow]No transcripts found in file.[/]",
            )
            return

        by_id = {t.id: t for t in transcripts}
        # load_all renames duplicates; if by_id is still short, something
        # pathological happened (shouldn't occur, but guard anyway).
        dup_shortfall = len(transcripts) - len(by_id)
        # Pre-compute table row data on background thread
        visible = transcripts[:_MAX_TABLE_ROWS]
        row_data = [
            (t.short_id, f"{t.length:,}", f"{t.gc_content:.1f}", t.id)
            for t in visible
        ]

        def _apply() -> None:
            try:
                self._apply_loaded_transcripts(
                    path, transcripts, by_id, row_data, visible, dup_shortfall,
                )
            except Exception:
                _log.exception("_apply_loaded_transcripts raised for %s", path)
                self._set_status(f"[red]Load succeeded but UI update failed (see log)[/]")

        self.call_from_thread(_apply)

    def _apply_loaded_transcripts(
        self,
        path: str,
        transcripts: list[Transcript],
        by_id: dict[str, Transcript],
        row_data: list[tuple[str, str, str, str]],
        visible: list[Transcript],
        dup_shortfall: int,
    ) -> None:
        """Main-thread tail of `_load_fasta` — split out so exceptions in any
        of the DOM/state mutations surface clearly in the log instead of
        silently eating the worker."""
        self.clear_notifications()
        self._transcripts = transcripts
        self._filtered = transcripts
        self._by_id = by_id
        self._fasta_path = path
        self._pfam_hits = {}
        # Clear stale per-transcript caches from the previous dataset.
        try:
            hmmer = self.query_one("#hmmer-panel")
            hmmer._scan_cache.clear()
            hmmer._confirm_cache.clear()
            hmmer._diagram_cache.clear()
            hmmer._last_shown_id = ""
            self.query_one("#seq-viewer", SequenceViewer)._last_shown_id = ""
            self.query_one("#stats-panel", StatsPanel)._last_shown_id = ""
        except Exception:
            _log.exception("Failed to clear per-panel caches")
        # Re-arm cancellation events so fresh scans can run again.
        _hmm_cancel.clear()
        _ncbi_blast_cancel.clear()
        # Invalidate per-transcript ORF cache — cheap and prevents cross-
        # session collisions on the (id, length, fingerprint) key.
        _longest_orf_cache.clear()
        self._populate_table_fast(row_data, len(transcripts), visible)
        try:
            self._refresh_transcriptome_select()
        except Exception:
            _log.exception("refresh_transcriptome_select raised during load apply")
        status_msg = f"[green]{len(transcripts):,} transcripts loaded from {path}[/]"
        if dup_shortfall:
            status_msg += f" [yellow](dedup anomaly: {dup_shortfall})[/]"
        self._set_status(status_msg)
        self.notify(
            f"{len(transcripts):,} transcripts loaded",
            title="Transcriptome", severity="information", timeout=3,
        )
        # Stats are no longer auto-computed — user triggers via the
        # "Compute Statistics" button in the Statistics tab.
        try:
            self.query_one("#stats-panel", StatsPanel).reset_for_new_dataset()
        except Exception:
            _log.exception("StatsPanel.reset_for_new_dataset raised")

    @work(exclusive=True, thread=True, group="stats")
    def _compute_stats_bg(self, transcripts: list[Transcript], path: str) -> None:
        """Two-phase stats: show basic numbers immediately, fill in ORF stats
        once the expensive 6-frame scan finishes."""
        # Phase 1: cheap length/GC/bucket stats (milliseconds even for 100k).
        basic = _compute_basic_stats(transcripts)

        def _apply_basic() -> None:
            self.query_one("#stats-panel", StatsPanel).render_stats(basic, path)

        self.call_from_thread(_apply_basic)
        if basic["n"] == 0:
            return

        # Phase 2: expensive ORF scan. Populates the per-transcript cache
        # along the way so subsequent clicks hit cached results instantly.
        orf_stats = _compute_orf_stats(transcripts)
        merged = {**basic, **orf_stats}

        def _apply_orf() -> None:
            self.query_one("#stats-panel", StatsPanel).render_stats(merged, path)

        self.call_from_thread(_apply_orf)

    def _populate_table_fast(
        self,
        row_data: list[tuple[str, str, str, str]],
        total: int,
        visible: list[Transcript],
        auto_select: bool = True,
    ) -> None:
        """Populate table from pre-computed row data (avoids GC calc on main thread)."""
        table = self.query_one("#transcript-table", DataTable)
        table.clear()
        for short_id, length, gc, tid in row_data:
            label = f"* {short_id}" if tid in self._bookmarks else short_id
            table.add_row(label, length, gc, key=tid)
        shown = len(row_data)
        count_text = f"{shown:,} of {total:,} shown" if shown < total else f"{total:,} shown"
        self.query_one("#transcript-count", Static).update(count_text)
        if visible and auto_select:
            table.focus()
            table.move_cursor(row=0)
            self._show_transcript(visible[0])

    def _populate_table(self, transcripts: list[Transcript], *, auto_select: bool = True) -> None:
        _log.debug("_populate_table: %d transcripts", len(transcripts))
        visible = transcripts[:_MAX_TABLE_ROWS]
        row_data = [
            (t.short_id, f"{t.length:,}", f"{t.gc_content:.1f}", t.id)
            for t in visible
        ]
        self._populate_table_fast(row_data, len(transcripts), visible, auto_select)

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
        raw = self.query_one("#filter-input", Input).value.strip()
        # Small lists: filter inline on the main thread (cheaper than a worker
        # handoff). Large lists: run the scan in a thread so keystrokes stay
        # responsive.
        if not raw:
            self._filtered = self._transcripts
            self._populate_table(self._filtered)
            return
        if len(self._transcripts) < 5000:
            self._filtered = _filter_transcripts(
                self._transcripts, raw, self._bookmarks,
            )
            self._populate_table(self._filtered)
        else:
            self._apply_filter_bg(raw, self._transcripts, self._bookmarks.copy())

    @work(exclusive=True, thread=True, group="filter")
    def _apply_filter_bg(
        self, raw: str, transcripts: list[Transcript], bookmarks: set[str],
    ) -> None:
        """Filter large transcriptomes off the main thread."""
        results = _filter_transcripts(transcripts, raw, bookmarks)
        visible = results[:_MAX_TABLE_ROWS]
        row_data = [
            (t.short_id, f"{t.length:,}", f"{t.gc_content:.1f}", t.id)
            for t in visible
        ]

        def _apply() -> None:
            # Bail if the filter text changed again while we were scanning —
            # a newer worker already queued the up-to-date results.
            current = self.query_one("#filter-input", Input).value.strip()
            if current != raw:
                return
            self._filtered = results
            self._populate_table_fast(row_data, len(results), visible)

        self.call_from_thread(_apply)

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    # ── Transcript selection ──────────────────────────────────────────────────

    @lru_cache(maxsize=1)
    def _panels(self) -> tuple[SequenceViewer, StatsPanel, BlastPanel, HmmerPanel]:
        return (
            self.query_one("#seq-viewer", SequenceViewer),
            self.query_one("#stats-panel", StatsPanel),
            self.query_one("#blast-panel", BlastPanel),
            self.query_one("#hmmer-panel", HmmerPanel),
        )

    _selection_timer: Timer | None = None
    _pending_transcript: Transcript | None = None
    _SELECTION_DEBOUNCE: float = 0.035  # 35 ms — below human perception

    def _show_transcript(self, t: Transcript) -> None:
        """Debounced panel update — coalesces rapid cursor movement.

        Arrow-key scrolling fires row-highlight events faster than panels
        can redraw; we stash the latest selection and only apply it after
        movement settles. A 35 ms delay is imperceptible on a single click
        but cuts redundant redraws dramatically on held arrow keys.
        """
        self._pending_transcript = t
        if self._selection_timer is not None:
            self._selection_timer.stop()
        self._selection_timer = self.set_timer(
            self._SELECTION_DEBOUNCE, self._apply_pending_transcript,
        )

    def _apply_pending_transcript(self) -> None:
        t = self._pending_transcript
        self._selection_timer = None
        if t is None:
            return
        try:
            sv, sp, bp, hp = self._panels()
            sv.transcript = t
            sp.transcript = t
            bp.transcript = t
            hp.transcript = t
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

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.data_table.id != "transcript-table":
            return
        col_map = {"ID": "id", "Length": "length", "GC%": "gc"}
        col_key = col_map.get(str(event.label), "")
        if not col_key:
            return
        if self._sort_column == col_key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = col_key
            self._sort_reverse = False
        key_funcs = {
            "id": lambda t: t.id.lower(),
            "length": lambda t: t.length,
            "gc": lambda t: t.gc_content,
        }
        self._filtered.sort(key=key_funcs[col_key], reverse=self._sort_reverse)
        self._populate_table(self._filtered, auto_select=False)

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
        # Look up the original subject_id via the BlastPanel mapping
        blast_panel = self.query_one("#blast-panel", BlastPanel)
        raw = str(row_key.value)
        subject_id = blast_panel._row_to_subject.get(raw, raw)
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
        # Highlight the hit region in the sequence viewer
        hit = blast_panel._row_to_hit.get(raw)
        if hit:
            sv = self.query_one("#seq-viewer", SequenceViewer)
            # subject_start/end are 1-based; convert to 0-based half-open
            lo = min(hit.subject_start, hit.subject_end) - 1
            hi = max(hit.subject_start, hit.subject_end)
            sv._highlight = (lo, hi)
            sv._focus_range = None  # BLAST highlight supersedes any prior focus
            sv.show_transcript(t)
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

        def _reset_build_btn(success: bool = False) -> None:
            try:
                btn = self.query_one("#blast-build-db", Button)
                btn.remove_class("building")
                if success:
                    btn.label = "DB Ready"
                    btn.variant = "success"
                else:
                    btn.label = "Build BLAST DB"
                    btn.variant = "warning"
            except Exception:
                pass

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
            _reset_build_btn(success=True)
        except RuntimeError as exc:
            self.clear_notifications()
            self._set_status(f"[red]makeblastdb error: {exc}[/]")
            self.notify(
                f"makeblastdb error: {exc}", title="BLAST",
                severity="error", timeout=5,
            )
            _reset_build_btn(success=False)

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

    _log_startup_banner()
    try:
        ScriptoScopeApp(startup_fasta=args.fasta or "").run()
    except Exception:
        _log.exception("App terminated with an unhandled exception")
        raise
    finally:
        _log.info("ScriptoScope session %s ending", _SESSION_ID)
    os._exit(0)


if __name__ == "__main__":
    main()
