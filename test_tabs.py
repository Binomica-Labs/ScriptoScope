"""
Automated tests for all ScriptoScope tabs using Textual's pilot API.
Uses a 20-transcript subset of pothosTranscriptome.fasta.
"""
import asyncio
import pytest
from pathlib import Path

from scriptoscope import (
    ScriptoScopeApp,
    Transcript,
    _compute_stats,
    _parse_fasta,
    colorize_sequence,
    load_all,
    SequenceViewer,
    StatsPanel,
    BlastPanel,
    HmmerPanel,
)
from textual.widgets import DataTable, Static, Input, TabbedContent, TabPane

TEST_FASTA = str(Path(__file__).parent / "test_subset.fasta")


# ── Unit tests for data model & helpers ──────────────────────────────────────

class TestFastaParsing:
    def test_load_all(self):
        transcripts = load_all(TEST_FASTA)
        assert len(transcripts) == 20
        assert all(isinstance(t, Transcript) for t in transcripts)

    def test_transcript_fields(self):
        transcripts = load_all(TEST_FASTA)
        t = transcripts[0]
        assert t.id == "contig_1_1"
        assert t.length > 0
        assert 0 <= t.gc_content <= 100
        counts = t.nucleotide_counts()
        assert sum(counts.values()) == t.length

    def test_short_id_truncation(self):
        t = Transcript(id="A" * 50, description="", sequence="ACGT")
        assert len(t.short_id) == 41  # 40 chars + ellipsis

    def test_short_id_no_truncation(self):
        t = Transcript(id="short", description="", sequence="ACGT")
        assert t.short_id == "short"


class TestComputeStats:
    def test_normal(self):
        transcripts = load_all(TEST_FASTA)
        stats = _compute_stats(transcripts)
        assert stats["n"] == 20
        assert stats["total_bases"] > 0
        assert stats["shortest"] <= stats["longest"]
        assert stats["mean_len"] > 0
        assert stats["n50"] > 0
        assert len(stats["bucket_counts"]) == 6

    def test_empty_transcripts(self):
        """This is the bug from the review — should not crash."""
        stats = _compute_stats([])
        assert stats["n"] == 0
        assert stats["total_bases"] == 0

    def test_single_transcript(self):
        t = Transcript(id="x", description="", sequence="ACGTACGT")
        stats = _compute_stats([t])
        assert stats["n"] == 1
        assert stats["mean_len"] == 8
        assert stats["n50"] == 8


class TestColorizeSequence:
    def test_short_sequence(self):
        result = colorize_sequence("ACGT", width=60)
        assert "ACGT" in result.plain

    def test_truncation(self):
        long_seq = "A" * 20_000
        result = colorize_sequence(long_seq, width=60)
        assert "more bases not shown" in result.plain

    def test_empty(self):
        result = colorize_sequence("", width=60)
        assert result.plain == ""


# ── TUI integration tests using Textual pilot ───────────────────────────────

class TestSequenceTab:
    @pytest.mark.asyncio
    async def test_app_loads_fasta(self):
        app = ScriptoScopeApp(startup_fasta=TEST_FASTA)
        async with app.run_test(size=(120, 40)) as pilot:
            # Wait for loading to complete
            await pilot.pause(2.0)

            # Check transcript table is populated
            table = app.query_one("#transcript-table", DataTable)
            assert table.row_count == 20, f"Expected 20 rows, got {table.row_count}"

            # Check that first transcript is auto-selected and displayed
            viewer = app.query_one("#seq-viewer", SequenceViewer)
            assert viewer.transcript is not None
            assert viewer.transcript.id == "contig_1_1"

            # Verify the sequence body has content (colorized sequence)
            body = app.query_one("#seq-body", Static)
            body_text = str(body.render())
            assert len(body_text) > 0, "Sequence body should have content"

    @pytest.mark.asyncio
    async def test_navigate_transcripts(self):
        app = ScriptoScopeApp(startup_fasta=TEST_FASTA)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)

            # Move cursor down to second transcript
            table = app.query_one("#transcript-table", DataTable)
            table.move_cursor(row=1)
            await pilot.pause(0.5)

            viewer = app.query_one("#seq-viewer", SequenceViewer)
            assert viewer.transcript is not None
            assert viewer.transcript.id == "contig_2_1"


class TestStatisticsTab:
    @pytest.mark.asyncio
    async def test_stats_display(self):
        app = ScriptoScopeApp(startup_fasta=TEST_FASTA)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)

            # Switch to statistics tab
            tabs = app.query_one("#content-area", TabbedContent)
            tabs.active = "tab-stats"
            await pilot.pause(0.5)

            panel = app.query_one("#stats-panel", StatsPanel)
            assert panel._global_stats is not None
            assert panel._global_stats["n"] == 20

            # Check that selected transcript is shown
            assert panel.transcript is not None


class TestBlastTab:
    @pytest.mark.asyncio
    async def test_blast_panel_renders(self):
        app = ScriptoScopeApp(startup_fasta=TEST_FASTA)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)

            # Switch to BLAST tab
            tabs = app.query_one("#content-area", TabbedContent)
            tabs.active = "tab-blast"
            await pilot.pause(0.5)

            panel = app.query_one("#blast-panel", BlastPanel)
            # Transcript should be set from sidebar selection
            assert panel.transcript is not None

            # BLAST results table should exist and be empty (no search run yet)
            blast_table = app.query_one("#blast-table", DataTable)
            assert blast_table.row_count == 0

    @pytest.mark.asyncio
    async def test_blast_no_transcript_warning(self):
        """Running BLAST with no transcript selected should show warning."""
        app = ScriptoScopeApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)

            panel = app.query_one("#blast-panel", BlastPanel)
            panel.transcript = None
            panel.run_blast()
            await pilot.pause(0.5)

            status = app.query_one("#blast-status", Static)
            assert "Select a transcript" in str(status.render())


class TestHmmerTab:
    @pytest.mark.asyncio
    async def test_hmmer_panel_renders(self):
        app = ScriptoScopeApp(startup_fasta=TEST_FASTA)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)

            # Switch to HMMER tab
            tabs = app.query_one("#content-area", TabbedContent)
            tabs.active = "tab-hmmer"
            await pilot.pause(0.5)

            panel = app.query_one("#hmmer-panel", HmmerPanel)
            assert panel.transcript is not None

            # HMMER results table should exist and be empty
            hmmer_table = app.query_one("#hmmer-table", DataTable)
            assert hmmer_table.row_count == 0

    @pytest.mark.asyncio
    async def test_hmmer_no_transcript_warning(self):
        app = ScriptoScopeApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)

            panel = app.query_one("#hmmer-panel", HmmerPanel)
            panel.transcript = None
            panel.run_scan()
            await pilot.pause(0.5)

            status = app.query_one("#hmmer-status", Static)
            assert "Select a transcript" in str(status.render())


class TestFiltering:
    @pytest.mark.asyncio
    async def test_filter_by_id(self):
        app = ScriptoScopeApp(startup_fasta=TEST_FASTA)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)

            # Type a filter
            filter_input = app.query_one("#filter-input", Input)
            filter_input.value = "contig_1"
            await pilot.pause(1.0)  # wait for debounce

            table = app.query_one("#transcript-table", DataTable)
            # Should match contig_1_1, contig_10_1, contig_11_1, etc.
            assert table.row_count > 0
            assert table.row_count < 20

    @pytest.mark.asyncio
    async def test_filter_empty_result(self):
        app = ScriptoScopeApp(startup_fasta=TEST_FASTA)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(2.0)

            filter_input = app.query_one("#filter-input", Input)
            filter_input.value = "nonexistent_contig_xyz"
            await pilot.pause(1.0)

            table = app.query_one("#transcript-table", DataTable)
            assert table.row_count == 0


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_no_file_startup(self):
        """App should start cleanly with no file."""
        app = ScriptoScopeApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(1.0)

            table = app.query_one("#transcript-table", DataTable)
            assert table.row_count == 0

            status = app.query_one("#app-status", Static)
            assert "No file loaded" in str(status.render())
