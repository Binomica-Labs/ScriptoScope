"""Smoke tests for ScriptoScope TUI.

Run with:  pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import asyncio
import csv
import os
import tempfile
from pathlib import Path

import pytest

# Ensure we can import from the repo root
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scriptoscope import (
    BlastHit,
    BlastPanel,
    HmmerHit,
    HmmerPanel,
    ORFCoord,
    ScriptoScopeApp,
    SequenceViewer,
    StatsPanel,
    Transcript,
    _build_aa_track,
    _build_feature_track,
    _build_pfam_track,
    _compute_stats,
    _export_csv,
    _filter_transcripts,
    _find_longest_orf,
    _parse_fasta,
    colorize_sequence_annotated,
    load_all,
)

from textual.widgets import Button, DataTable, Input, Static

# ── Test data ────────────────────────────────────────────────────────────────

# Small test FASTA with a known ATG-containing ORF
_TEST_FASTA = """\
>transcript_001 ribosomal protein S1
ATGAAAGCTTTTGGGCCCAAATTTGATCCCAAATTTGGGAAATTTCCCGATCCCAAAGGGAAATTTCCCGATAAAGGGTTTCCCAAATTTGGGCCCTAATAG
>transcript_002 hypothetical protein
GCATGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCATGCTAGCTAGC
>transcript_003 elongation factor Tu len=300
ATGAAAGCTTTTGGGCCCAAATTTGATCCCAAATTTGGGAAATTTCCCGATCCCAAAGGGAAATTTCCCGATAAAGGGTTTCCCAAATTTGGGCCCTAATAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAATTTCCCGATCCCAAAGGGAAATTTCCCGATAAAGGGTTTCCCAAATTTGGGCCC
"""


@pytest.fixture
def fasta_path(tmp_path: Path) -> str:
    """Write test FASTA to a temp file and return the path."""
    p = tmp_path / "test.fasta"
    p.write_text(_TEST_FASTA)
    return str(p)


@pytest.fixture
def transcripts(fasta_path: str) -> list[Transcript]:
    return load_all(fasta_path)


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — pure functions (no TUI)
# ══════════════════════════════════════════════════════════════════════════════


class TestFastaLoader:
    def test_parse_fasta_count(self, fasta_path: str):
        transcripts = load_all(fasta_path)
        assert len(transcripts) == 3

    def test_parse_fasta_ids(self, fasta_path: str):
        transcripts = load_all(fasta_path)
        assert transcripts[0].id == "transcript_001"
        assert transcripts[1].id == "transcript_002"
        assert transcripts[2].id == "transcript_003"

    def test_parse_fasta_descriptions(self, fasta_path: str):
        transcripts = load_all(fasta_path)
        assert "ribosomal" in transcripts[0].description

    def test_transcript_length(self, transcripts: list[Transcript]):
        assert transcripts[0].length == len(transcripts[0].sequence)
        assert transcripts[0].length > 0

    def test_transcript_gc_content(self, transcripts: list[Transcript]):
        gc = transcripts[0].gc_content
        assert 0.0 <= gc <= 100.0

    def test_transcript_short_id(self, transcripts: list[Transcript]):
        sid = transcripts[0].short_id
        assert isinstance(sid, str)
        assert len(sid) <= 41  # 40 + potential "…"

    def test_nucleotide_counts(self, transcripts: list[Transcript]):
        counts = transcripts[0].nucleotide_counts()
        assert set(counts.keys()) == {"A", "C", "G", "T", "N"}
        assert sum(counts.values()) == transcripts[0].length

    def test_empty_fasta(self, tmp_path: Path):
        p = tmp_path / "empty.fasta"
        p.write_text("")
        assert load_all(str(p)) == []


class TestComputeStats:
    def test_basic_stats(self, transcripts: list[Transcript]):
        stats = _compute_stats(transcripts)
        assert stats["n"] == 3
        assert stats["total_bases"] > 0
        assert stats["shortest"] > 0
        assert stats["longest"] >= stats["shortest"]
        assert stats["mean_len"] > 0
        assert stats["median_len"] > 0
        assert stats["n50"] > 0
        assert 0.0 <= stats["mean_gc"] <= 100.0

    def test_bucket_counts(self, transcripts: list[Transcript]):
        stats = _compute_stats(transcripts)
        assert len(stats["bucket_counts"]) == 6
        assert sum(stats["bucket_counts"]) == 3

    def test_orf_stats(self, transcripts: list[Transcript]):
        stats = _compute_stats(transcripts)
        assert "orf_count" in stats
        assert "orf_lengths" in stats
        assert stats["orf_count"] >= 0
        assert stats["orf_count"] == len(stats["orf_lengths"])

    def test_empty_stats(self):
        stats = _compute_stats([])
        assert stats["n"] == 0


class TestFilterTranscripts:
    def test_text_filter_by_id(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "001")
        assert len(result) == 1
        assert result[0].id == "transcript_001"

    def test_text_filter_by_description(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "ribosomal")
        assert len(result) == 1
        assert result[0].id == "transcript_001"

    def test_text_filter_case_insensitive(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "RIBOSOMAL")
        assert len(result) == 1

    def test_length_filter_gt(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "len>100")
        assert all(t.length > 100 for t in result)

    def test_length_filter_lt(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "len<60")
        assert all(t.length < 60 for t in result)

    def test_gc_filter(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "gc>30")
        assert all(t.gc_content > 30 for t in result)

    def test_combined_filter(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "transcript len>40")
        assert all(t.length > 40 for t in result)
        assert all("transcript" in t.id.lower() for t in result)

    def test_bookmark_filter(self, transcripts: list[Transcript]):
        bookmarks = {"transcript_001"}
        result = _filter_transcripts(transcripts, "bookmarked", bookmarks)
        assert len(result) == 1
        assert result[0].id == "transcript_001"

    def test_no_match(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "nonexistent_xyz_999")
        assert len(result) == 0

    def test_empty_query(self, transcripts: list[Transcript]):
        result = _filter_transcripts(transcripts, "")
        assert len(result) == 3


class TestORFDetection:
    def test_find_longest_orf(self, transcripts: list[Transcript]):
        orf = _find_longest_orf(transcripts[0].sequence, transcripts[0].id)
        assert orf is not None
        assert orf.aa_length > 0
        assert orf.strand in ("+", "-")
        assert orf.frame in (1, 2, 3)

    def test_orf_with_start_codon(self, transcripts: list[Transcript]):
        # transcript_001 starts with ATG
        orf = _find_longest_orf(transcripts[0].sequence, transcripts[0].id)
        if orf and orf.sequence:
            assert orf.sequence[0] == "M"  # methionine


class TestSequenceRendering:
    def test_colorize_no_orf(self, transcripts: list[Transcript]):
        result = colorize_sequence_annotated(transcripts[0].sequence)
        assert result.text is not None
        assert len(result.line_map) > 0

    def test_colorize_with_orf(self, transcripts: list[Transcript]):
        orf = _find_longest_orf(transcripts[0].sequence, transcripts[0].id)
        if orf:
            result = colorize_sequence_annotated(transcripts[0].sequence, orf=orf)
            assert result.text is not None
            assert any(li.line_type == "dna" for li in result.line_map)
            assert any(li.line_type == "feat" for li in result.line_map)

    def test_build_aa_track(self):
        orf = ORFCoord(
            orf_id="test", strand="+", frame=1,
            nt_start=0, nt_end=9, aa_length=3,
            sequence="MKA",
        )
        n = 12
        aa_at = [None] * n
        aa_color = [""] * n
        base_override = [None] * n
        _build_aa_track(orf, n, aa_at, aa_color, base_override)
        # Center of first codon (pos 1) should have "M"
        assert aa_at[1] == "M"

    def test_build_feature_track(self):
        orf = ORFCoord(
            orf_id="test", strand="+", frame=1,
            nt_start=0, nt_end=30, aa_length=10,
            sequence="M" * 10,
        )
        n = 30
        feat_ch = [None] * n
        feat_color = [""] * n
        _build_feature_track(orf, n, feat_ch, feat_color)
        assert any(ch is not None for ch in feat_ch)


class TestCSVExport:
    def test_export_csv(self, tmp_path: Path):
        path = str(tmp_path / "test.csv")
        headers = ["Name", "Value"]
        rows = [["foo", "1"], ["bar", "2"]]
        _export_csv(path, headers, rows)

        with open(path) as f:
            reader = csv.reader(f)
            lines = list(reader)
        assert lines[0] == ["Name", "Value"]
        assert lines[1] == ["foo", "1"]
        assert lines[2] == ["bar", "2"]


# ══════════════════════════════════════════════════════════════════════════════
# TUI integration tests — full app with Pilot
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def app(fasta_path: str) -> ScriptoScopeApp:
    """Create an app instance with a test FASTA pre-loaded."""
    return ScriptoScopeApp(startup_fasta=fasta_path)


class TestAppStartup:
    @pytest.mark.asyncio
    async def test_app_mounts(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)  # Wait for FASTA load
            assert app.query_one("#transcript-table", DataTable) is not None
            assert app.query_one("#seq-viewer", SequenceViewer) is not None
            assert app.query_one("#blast-panel", BlastPanel) is not None
            assert app.query_one("#hmmer-panel", HmmerPanel) is not None
            assert app.query_one("#stats-panel", StatsPanel) is not None

    @pytest.mark.asyncio
    async def test_fasta_loaded(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            assert len(app._transcripts) == 3
            assert len(app._by_id) == 3

    @pytest.mark.asyncio
    async def test_table_populated(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            table = app.query_one("#transcript-table", DataTable)
            assert table.row_count == 3

    @pytest.mark.asyncio
    async def test_transcript_count_label(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            count = app.query_one("#transcript-count", Static)
            text = count.content
            assert "3" in text


class TestTranscriptSelection:
    @pytest.mark.asyncio
    async def test_first_transcript_selected(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            sv = app.query_one("#seq-viewer", SequenceViewer)
            assert sv.transcript is not None
            assert sv.transcript.id == "transcript_001"

    @pytest.mark.asyncio
    async def test_navigate_with_arrows(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            table = app.query_one("#transcript-table", DataTable)
            table.focus()
            await pilot.press("down")
            await pilot.pause(0.5)
            sv = app.query_one("#seq-viewer", SequenceViewer)
            assert sv.transcript is not None
            assert sv.transcript.id == "transcript_002"


class TestFiltering:
    @pytest.mark.asyncio
    async def test_filter_by_id(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            inp = app.query_one("#filter-input", Input)
            inp.focus()
            inp.value = "001"
            await pilot.pause(0.5)
            table = app.query_one("#transcript-table", DataTable)
            assert table.row_count == 1

    @pytest.mark.asyncio
    async def test_filter_by_description(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            inp = app.query_one("#filter-input", Input)
            inp.focus()
            inp.value = "ribosomal"
            await pilot.pause(0.5)
            table = app.query_one("#transcript-table", DataTable)
            assert table.row_count == 1

    @pytest.mark.asyncio
    async def test_filter_by_length(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            inp = app.query_one("#filter-input", Input)
            inp.focus()
            inp.value = "len>100"
            await pilot.pause(0.5)
            table = app.query_one("#transcript-table", DataTable)
            # Only transcripts > 100bp should remain
            assert table.row_count < 3

    @pytest.mark.asyncio
    async def test_filter_clear(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            inp = app.query_one("#filter-input", Input)
            inp.focus()
            inp.value = "001"
            await pilot.pause(1.0)
            inp.value = ""
            await pilot.pause(1.0)
            table = app.query_one("#transcript-table", DataTable)
            assert table.row_count == 3


class TestColumnSorting:
    @pytest.mark.asyncio
    async def test_sort_by_length(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            table = app.query_one("#transcript-table", DataTable)
            # Simulate clicking "Length" header
            table.focus()
            # Use the app's handler directly since clicking headers is tricky
            app._sort_column = "length"
            app._sort_reverse = False
            app._filtered.sort(key=lambda t: t.length)
            app._populate_table(app._filtered, auto_select=False)
            await pilot.pause(0.3)
            # Verify table still has all rows
            assert table.row_count == 3

    @pytest.mark.asyncio
    async def test_sort_reverse(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            # Sort ascending then descending
            app._sort_column = "length"
            app._sort_reverse = False
            app._filtered.sort(key=lambda t: t.length)
            first_asc = app._filtered[0].id

            app._sort_reverse = True
            app._filtered.sort(key=lambda t: t.length, reverse=True)
            first_desc = app._filtered[0].id

            # Reversed should give a different first element (if lengths differ)
            if app._filtered[0].length != app._filtered[-1].length:
                assert first_asc != first_desc


class TestBookmarks:
    @pytest.mark.asyncio
    async def test_toggle_bookmark(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            table = app.query_one("#transcript-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause(0.3)

            assert "transcript_001" not in app._bookmarks
            app.action_toggle_bookmark()
            await pilot.pause(0.3)
            assert "transcript_001" in app._bookmarks

            # Toggle off
            app.action_toggle_bookmark()
            await pilot.pause(0.3)
            assert "transcript_001" not in app._bookmarks

    @pytest.mark.asyncio
    async def test_bookmark_filter(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            table = app.query_one("#transcript-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause(0.3)

            app.action_toggle_bookmark()
            await pilot.pause(0.3)

            inp = app.query_one("#filter-input", Input)
            inp.focus()
            inp.value = "bookmarked"
            await pilot.pause(0.5)
            assert table.row_count == 1

    @pytest.mark.asyncio
    async def test_export_bookmarked_fasta(self, app: ScriptoScopeApp, tmp_path: Path):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            table = app.query_one("#transcript-table", DataTable)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause(0.3)
            app.action_toggle_bookmark()
            await pilot.pause(0.3)

            # Override export path
            export_path = str(tmp_path / "export.fasta")
            count = 0
            with open(export_path, "w") as f:
                for tid in app._bookmarks:
                    t = app._by_id.get(tid)
                    if t:
                        f.write(f">{t.id} {t.description}\n")
                        f.write(t.sequence + "\n")
                        count += 1
            assert count == 1
            assert Path(export_path).exists()
            content = Path(export_path).read_text()
            assert ">transcript_001" in content


class TestHelpScreen:
    @pytest.mark.asyncio
    async def test_help_opens(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            app.action_show_help()
            await pilot.pause(0.5)
            # Help modal should be on the screen stack
            assert len(app.screen_stack) > 1

    @pytest.mark.asyncio
    async def test_help_closes_on_escape(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            app.action_show_help()
            await pilot.pause(0.5)
            await pilot.press("escape")
            await pilot.pause(0.5)
            assert len(app.screen_stack) == 1


class TestSequenceViewer:
    @pytest.mark.asyncio
    async def test_sequence_displayed(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            sv = app.query_one("#seq-viewer", SequenceViewer)
            assert sv.transcript is not None
            body = sv.query_one("#seq-body", Static)
            # Body should have some rendered content
            text = body.content
            assert len(text) > 0

    @pytest.mark.asyncio
    async def test_goto_position_widget_exists(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            goto_input = app.query_one("#seq-goto-input", Input)
            assert goto_input is not None


class TestBlastPanel:
    @pytest.mark.asyncio
    async def test_blast_panel_exists(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            bp = app.query_one("#blast-panel", BlastPanel)
            assert bp is not None
            assert bp.query_one("#blast-run", Button) is not None
            assert bp.query_one("#blast-ncbi", Button) is not None
            assert bp.query_one("#blast-export", Button) is not None
            assert bp.query_one("#blast-build-db", Button) is not None

    @pytest.mark.asyncio
    async def test_blast_export_empty(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            bp = app.query_one("#blast-panel", BlastPanel)
            # Export with no results should show warning
            bp._export_blast()
            await pilot.pause(0.3)
            status = bp.query_one("#blast-status", Static)
            text = status.content
            assert "No BLAST results" in text


class TestHmmerPanel:
    @pytest.mark.asyncio
    async def test_hmmer_panel_exists(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            hp = app.query_one("#hmmer-panel", HmmerPanel)
            assert hp is not None
            assert hp.query_one("#hmmer-run", Button) is not None
            assert hp.query_one("#hmmer-export", Button) is not None

    @pytest.mark.asyncio
    async def test_hmmer_export_empty(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            hp = app.query_one("#hmmer-panel", HmmerPanel)
            hp._export_hmmer()
            await pilot.pause(0.3)
            status = hp.query_one("#hmmer-status", Static)
            text = status.content
            assert "No HMMER results" in text


class TestStatsPanel:
    @pytest.mark.asyncio
    async def test_stats_not_auto_computed(self, app: ScriptoScopeApp):
        """Stats should NOT auto-run on load — user must press the button."""
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            sp = app.query_one("#stats-panel", StatsPanel)
            assert sp._global_stats is None

    @pytest.mark.asyncio
    async def test_stats_panel_computes_on_button_press(self, app: ScriptoScopeApp):
        """Pressing Compute Statistics kicks off the two-phase computation."""
        from textual.widgets import TabbedContent
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            # Switch to the Statistics tab so the button is interactable
            app.query_one("#content-area", TabbedContent).active = "tab-stats"
            await pilot.pause(0.2)
            sp = app.query_one("#stats-panel", StatsPanel)
            compute_btn = sp.query_one("#stats-compute", Button)
            assert compute_btn.disabled is False
            # Trigger the handler directly — pilot.click through a tab pane is
            # timing-sensitive, but the handler is a thin wrapper anyway.
            sp._on_compute_pressed()
            await pilot.pause(3.0)  # Let both phases finish
            assert sp._global_stats is not None
            assert sp._global_stats["n"] == 3
            # Export should be enabled after stats are available
            assert sp.query_one("#stats-export", Button).disabled is False

    @pytest.mark.asyncio
    async def test_stats_export_button_exists(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            sp = app.query_one("#stats-panel", StatsPanel)
            export_btn = sp.query_one("#stats-export", Button)
            assert export_btn is not None
            # Export button should start disabled (no stats computed yet)
            assert export_btn.disabled is True


class TestCSVExportIntegration:
    @pytest.mark.asyncio
    async def test_blast_export_with_data(self, app: ScriptoScopeApp, tmp_path: Path):
        """Test BLAST CSV export after manually adding hits."""
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            bp = app.query_one("#blast-panel", BlastPanel)
            table = bp.query_one("#blast-table", DataTable)
            # Manually add a row to simulate a BLAST result
            table.add_row("subject1", "95.0", "100", "1e-10", "200.0",
                          "1", "100", "1", "100", key="test_row")
            bp._row_to_subject["test_row"] = "subject1"
            await pilot.pause(0.3)

            # Override export path for test
            export_path = str(tmp_path / "blast.csv")
            headers = ["Subject", "% ID", "Aln Len", "E-value", "Bit Score",
                        "Q Start", "Q End", "S Start", "S End"]
            rows = []
            for row_key in table.rows:
                row = table.get_row(row_key)
                rows.append([str(c) for c in row])
            _export_csv(export_path, headers, rows)

            assert Path(export_path).exists()
            with open(export_path) as f:
                reader = csv.reader(f)
                lines = list(reader)
            assert len(lines) == 2  # header + 1 row
            assert lines[1][0] == "subject1"


class TestTabNavigation:
    @pytest.mark.asyncio
    async def test_switch_to_blast_tab(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            from textual.widgets import TabbedContent
            tabs = app.query_one("#content-area", TabbedContent)
            tabs.active = "tab-blast"
            await pilot.pause(0.3)
            assert tabs.active == "tab-blast"

    @pytest.mark.asyncio
    async def test_switch_to_hmmer_tab(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            from textual.widgets import TabbedContent
            tabs = app.query_one("#content-area", TabbedContent)
            tabs.active = "tab-hmmer"
            await pilot.pause(0.3)
            assert tabs.active == "tab-hmmer"

    @pytest.mark.asyncio
    async def test_switch_to_stats_tab(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            from textual.widgets import TabbedContent
            tabs = app.query_one("#content-area", TabbedContent)
            tabs.active = "tab-stats"
            await pilot.pause(0.3)
            assert tabs.active == "tab-stats"


class TestAppState:
    @pytest.mark.asyncio
    async def test_initial_bookmarks_empty(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            assert len(app._bookmarks) == 0

    @pytest.mark.asyncio
    async def test_initial_sort_empty(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)
            assert app._sort_column == ""
            assert app._sort_reverse is False

    @pytest.mark.asyncio
    async def test_filtered_matches_all(self, app: ScriptoScopeApp):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)
            assert len(app._filtered) == len(app._transcripts)
