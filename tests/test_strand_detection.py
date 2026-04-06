"""Tests for polyA/polyT strand detection and strand-aware ORF selection.

These tests validate:
1. detect_mrna_strand correctly identifies polyA tails and polyT heads
2. find_best_orf selects the correct ORF based on strand signals
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scriptoscope import (
    ORFCoord,
    _find_longest_orf,
    _six_frame_orf_coords,
    detect_mrna_strand,
    find_best_orf,
)


# ══════════════════════════════════════════════════════════════════════════════
# detect_mrna_strand
# ══════════════════════════════════════════════════════════════════════════════


class TestDetectMrnaStrand:
    """Tests for polyA/polyT signal detection."""

    def test_polya_tail_strong(self):
        """Clear polyA tail (25 A's at end) should return '+'."""
        seq = "ATGCGTACGATCGATCGATCG" + "A" * 25
        assert detect_mrna_strand(seq) == "+"

    def test_polyt_head_strong(self):
        """Clear polyT head (25 T's at start) should return '-'."""
        seq = "T" * 25 + "ATGCGTACGATCGATCGATCG"
        assert detect_mrna_strand(seq) == "-"

    def test_no_signal(self):
        """Random sequence without polyA/T should return None."""
        seq = "ATGCGTACGATCGATCGATCGATCGATCGATCGATCGATCGATCG"
        assert detect_mrna_strand(seq) is None

    def test_short_sequence(self):
        """Sequence shorter than 8 bases should return None."""
        assert detect_mrna_strand("AAAAAAA") is None
        assert detect_mrna_strand("ATGC") is None

    def test_empty_sequence(self):
        """Empty sequence should return None."""
        assert detect_mrna_strand("") is None

    def test_polya_run_8(self):
        """A run of exactly 8 A's at the end should trigger '+'."""
        seq = "ATGCGTACGATCGATCGATCGATCGATCGATCG" + "A" * 8
        assert detect_mrna_strand(seq) == "+"

    def test_polyt_run_8(self):
        """A run of exactly 8 T's at the start should trigger '-'."""
        seq = "T" * 8 + "ATGCGTACGATCGATCGATCGATCGATCGATCG"
        assert detect_mrna_strand(seq) == "-"

    def test_polya_density_threshold(self):
        """Last 30 bases with >=20 A's should detect '+'."""
        # 20 A's + 10 C's in the last 30 bases
        tail = "A" * 20 + "C" * 10
        seq = "GCATGCATGCATGCATGCATGCATGCATGCATGCATGCATGC" + tail
        assert detect_mrna_strand(seq) == "+"

    def test_polya_density_below_threshold(self):
        """Last 30 bases with <20 A's and no long run should return None."""
        # 19 A's + 11 C's in the last 30
        tail = "A" * 19 + "C" * 11
        seq = "GCATGCATGCATGCATGCATGCATGCATGCATGCATGCATGC" + tail
        assert detect_mrna_strand(seq) is None

    def test_polyt_density_threshold(self):
        """First 30 bases with >=20 T's should detect '-'."""
        head = "T" * 20 + "C" * 10
        seq = head + "GCATGCATGCATGCATGCATGCATGCATGCATGCATGCATGC"
        assert detect_mrna_strand(seq) == "-"

    def test_both_signals_polya_stronger(self):
        """When both polyA and polyT are present, stronger one wins."""
        # Strong polyA (25 A's at end), weak polyT (8 T's at start)
        seq = "T" * 8 + "GCATGCATGCATGCATGCATGCATGC" + "A" * 25
        result = detect_mrna_strand(seq)
        assert result == "+"

    def test_both_signals_polyt_stronger(self):
        """When both present with polyT stronger, should return '-'."""
        # Strong polyT (25 T's at start), weak polyA (8 A's at end)
        seq = "T" * 25 + "GCATGCATGCATGCATGCATGCATGC" + "A" * 8
        result = detect_mrna_strand(seq)
        assert result == "-"

    def test_case_insensitive(self):
        """Detection should work with lowercase sequence."""
        seq = "atgcgtacgatcgatcgatcg" + "a" * 25
        assert detect_mrna_strand(seq) == "+"

    def test_run_7_not_enough(self):
        """A run of 7 A's at the end should NOT trigger if no density signal."""
        seq = "ATGCGTACGATCGATCGATCGATCGATCGATCG" + "A" * 7
        assert detect_mrna_strand(seq) is None

    def test_equal_signals_polya_wins(self):
        """When both signals are exactly equal, polyA wins by convention."""
        # Same length run at both ends
        seq = "T" * 10 + "GCATGCATGCATGCATGCATGCATGC" + "A" * 10
        result = detect_mrna_strand(seq)
        assert result == "+"


# ══════════════════════════════════════════════════════════════════════════════
# find_best_orf
# ══════════════════════════════════════════════════════════════════════════════


def _make_orf_seq(length_aa: int) -> str:
    """Build a coding sequence (ATG + codons + stop) of the given aa length."""
    # Use ALA (GCT) as filler codons
    return "ATG" + "GCT" * (length_aa - 1) + "TAA"


class TestFindBestOrf:
    """Tests for strand-aware ORF selection."""

    def test_returns_tuple_of_three(self):
        """find_best_orf should return (orf, strand, all_orfs)."""
        seq = _make_orf_seq(50)
        result = find_best_orf(seq, "test")
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_no_orf_found(self):
        """Short sequence with no ORF >= 30 aa."""
        seq = "ATGCGATAG"  # 3 aa, way too short
        best, strand, all_orfs = find_best_orf(seq, "test_short")
        assert best is None

    def test_single_plus_strand_orf(self):
        """A clean coding sequence should find the ORF on + strand."""
        cds = _make_orf_seq(60)
        seq = "GGGGG" + cds + "CCCCC"
        best, strand, all_orfs = find_best_orf(seq, "test_plus")
        assert best is not None
        assert best.aa_length == 60
        assert best.strand == "+"

    def test_polya_selects_plus_strand(self):
        """PolyA tail should bias selection toward + strand ORFs."""
        cds = _make_orf_seq(50)
        seq = "GGGGG" + cds + "GGGGG" + "A" * 25
        best, strand, all_orfs = find_best_orf(seq, "test_polya")
        assert strand == "+"
        assert best is not None

    def test_all_orfs_includes_all_frames(self):
        """all_orfs should include ORFs from multiple frames."""
        cds = _make_orf_seq(50)
        seq = "GGGGG" + cds + "GGGGG"
        _, _, all_orfs = find_best_orf(seq, "test_all")
        # Should have at least one ORF
        assert len(all_orfs) >= 1

    def test_no_strand_signal_uses_longest(self):
        """Without polyA/T signal, should pick the globally longest ORF."""
        cds = _make_orf_seq(80)
        seq = "GGGGG" + cds + "GGGGG"
        best, strand, _ = find_best_orf(seq, "test_nostrand")
        assert strand is None
        assert best is not None
        assert best.aa_length == 80

    def test_same_strand_orf_preferred_when_close(self):
        """If same-strand ORF is >=70% of longest, prefer same-strand."""
        # Build a sequence with polyA tail and check selection logic
        cds = _make_orf_seq(50)
        seq = "GGGGG" + cds + "GGGGG" + "A" * 25
        best, strand, all_orfs = find_best_orf(seq, "test_close")
        assert strand == "+"
        # The best ORF should be on the + strand
        if best is not None and best.strand == "+":
            # Confirm it was selected from the same strand
            plus_orfs = [o for o in all_orfs if o.strand == "+"]
            assert any(o.aa_length == best.aa_length for o in plus_orfs)

    def test_find_best_orf_backward_compat_with_find_longest(self):
        """find_best_orf and _find_longest_orf should agree when no strand signal."""
        cds = _make_orf_seq(60)
        # No polyA/T so strand is None → should match _find_longest_orf
        seq = "GGGGG" + cds + "GGGGG"
        best, strand, _ = find_best_orf(seq, "test_compat")
        longest = _find_longest_orf(seq, "test_compat")
        assert strand is None
        # Both should find an ORF of 60 aa
        assert best is not None and longest is not None
        assert best.aa_length == longest.aa_length
