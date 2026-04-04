"""Sacred tests for codon usage and DNA parsing correctness.

These tests are the last line of defense against silent data corruption.
A bioinformatics tool that mistranslates codons or miscounts bases is worse
than useless — it produces plausible-looking wrong answers that downstream
analyses depend on.

Every assertion here is cross-validated against Biopython's authoritative
standard genetic code table or constructed from hand-verifiable ground truth
with exact expected values. If any of these fail, do NOT ship.
"""
from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scriptoscope import (
    Transcript,
    _CODON_SCAN_RE,
    _RC_TABLE,
    _find_longest_orf,
    _longest_orf_aa_length,
    _parse_fasta,
    _six_frame_orf_coords,
    load_all,
)


# ══════════════════════════════════════════════════════════════════════════════
# Ground truth: the standard genetic code, cross-checked against Biopython
# ══════════════════════════════════════════════════════════════════════════════

# All 64 possible codons from the 4-letter DNA alphabet.
ALL_CODONS = ["".join(c) for c in itertools.product("ACGT", repeat=3)]

# The ONLY stop codons in the standard genetic code (NCBI table 1).
STANDARD_STOPS = {"TAA", "TAG", "TGA"}

# The canonical start codon in the standard genetic code.
STANDARD_START = "ATG"


class TestGeneticCodeGroundTruth:
    """Cross-check our hard-coded codon knowledge against Biopython."""

    def test_stop_codons_match_biopython(self):
        """The regex's stop set must exactly equal Biopython's standard stops."""
        from Bio.Data.CodonTable import standard_dna_table
        assert set(standard_dna_table.stop_codons) == STANDARD_STOPS, (
            f"Biopython reports stop codons {set(standard_dna_table.stop_codons)}, "
            f"we assume {STANDARD_STOPS}"
        )

    def test_atg_translates_to_methionine(self):
        """ATG must encode Met (M) in the standard code."""
        from Bio.Data.CodonTable import standard_dna_table
        assert standard_dna_table.forward_table["ATG"] == "M"

    def test_stops_are_not_in_forward_table(self):
        """Stop codons must not translate to any amino acid."""
        from Bio.Data.CodonTable import standard_dna_table
        for stop in STANDARD_STOPS:
            assert stop not in standard_dna_table.forward_table, (
                f"{stop} should be a stop codon, not {standard_dna_table.forward_table.get(stop)}"
            )

    def test_standard_start_is_canonical(self):
        """ATG must be a recognized start in the standard code (alongside alternates)."""
        from Bio.Data.CodonTable import standard_dna_table
        assert STANDARD_START in standard_dna_table.start_codons


class TestCodonScanRegex:
    """The regex in _longest_orf_aa_length is the single source of truth for
    start/stop codon recognition. One typo here silently breaks everything."""

    def test_regex_matches_only_start_and_stops(self):
        """The regex must match exactly {ATG, TAA, TAG, TGA} and nothing else."""
        expected = {"ATG"} | STANDARD_STOPS
        for codon in ALL_CODONS:
            matches = _CODON_SCAN_RE.findall(codon)
            if codon in expected:
                assert matches == [codon], (
                    f"Expected {codon} to match; got {matches}"
                )
            else:
                assert matches == [], (
                    f"Expected {codon} not to match; got {matches}"
                )

    def test_regex_finds_overlapping_matches_across_frames(self):
        """TAATG has a stop at 0 (frame 0) AND an ATG at 2 (frame 2).
        Without the lookahead, the ATG would be hidden by the TAA match."""
        matches = [(m.start(), m.group(1)) for m in _CODON_SCAN_RE.finditer("TAATG")]
        assert (0, "TAA") in matches
        assert (2, "ATG") in matches

    def test_regex_finds_back_to_back_stops(self):
        """Consecutive stops must all be found."""
        matches = [(m.start(), m.group(1)) for m in _CODON_SCAN_RE.finditer("TAATAGTGA")]
        assert (0, "TAA") in matches
        assert (3, "TAG") in matches
        assert (6, "TGA") in matches


class TestReverseComplement:
    """RC is trivial to get wrong. Verify base-by-base against Biopython."""

    def test_rc_table_matches_biopython(self):
        from Bio.Seq import Seq
        for seq in [
            "ACGT",
            "AAAA",
            "GCAT",
            "ATGAAAGCTTTTGGG",
            "N" * 10,
            "ACGTN" * 20,
        ]:
            mine = seq.translate(_RC_TABLE)[::-1]
            theirs = str(Seq(seq).reverse_complement())
            assert mine == theirs, f"RC mismatch on {seq}: {mine} vs {theirs}"

    def test_rc_is_involutive(self):
        """rc(rc(x)) == x for every sequence we support."""
        random.seed(0xC0DE)
        for _ in range(100):
            length = random.randint(1, 500)
            seq = "".join(random.choices("ACGTN", k=length))
            once = seq.translate(_RC_TABLE)[::-1]
            twice = once.translate(_RC_TABLE)[::-1]
            assert twice == seq


class TestORFLengthHandCrafted:
    """Ground-truth ORF scanning on sequences where the correct answer is
    trivially derivable by hand."""

    def test_no_orf_in_empty_sequence(self):
        assert _longest_orf_aa_length("") == 0

    def test_no_orf_below_threshold(self):
        # M + 5 K + stop = 6 aa, below min_aa=30
        seq = "ATG" + "AAA" * 5 + "TAA"
        assert _longest_orf_aa_length(seq) == 0

    def test_exact_length_at_threshold(self):
        # M + 29 K + stop = 30 aa, exactly at threshold
        seq = "ATG" + "AAA" * 29 + "TAA"
        assert _longest_orf_aa_length(seq) == 30

    def test_exact_length_41_aa(self):
        # M + 40 K + stop = 41 aa
        seq = "ATG" + "AAA" * 40 + "TAA"
        assert _longest_orf_aa_length(seq) == 41

    @pytest.mark.parametrize("stop", ["TAA", "TAG", "TGA"])
    def test_each_stop_codon_terminates_orf(self, stop: str):
        """Every canonical stop must terminate an ORF at the same length."""
        seq = "ATG" + "AAA" * 40 + stop
        assert _longest_orf_aa_length(seq) == 41, (
            f"stop codon {stop} did not terminate the ORF correctly"
        )

    @pytest.mark.parametrize("non_stop", [
        "TAT",  # Tyr — easy to confuse with TAA/TAG
        "TGT",  # Cys — easy to confuse with TGA
        "TGG",  # Trp — easy to confuse with TGA
        "CAA",  # Gln
        "AAA",  # Lys
        "GGG",  # Gly
    ])
    def test_non_stop_codons_do_not_terminate(self, non_stop: str):
        """Codons that look superficially similar to stops must NOT stop."""
        # ORF of 31 aa if the non-stop were (wrongly) treated as a stop,
        # 41 aa if it's correctly passed through.
        seq = "ATG" + "AAA" * 30 + non_stop + "AAA" * 9 + "TAA"
        assert _longest_orf_aa_length(seq) == 41, (
            f"{non_stop} was wrongly treated as a stop codon"
        )

    def test_orf_in_frame_2(self):
        # One leading base → frame shifts to 1
        seq = "A" + "ATG" + "AAA" * 40 + "TAA"
        assert _longest_orf_aa_length(seq) == 41

    def test_orf_in_frame_3(self):
        # Two leading bases → frame shifts to 2
        seq = "AA" + "ATG" + "AAA" * 40 + "TAA"
        assert _longest_orf_aa_length(seq) == 41

    def test_multiple_orfs_returns_longest(self):
        short = "ATG" + "AAA" * 35 + "TAA"  # 36 aa
        long_ = "ATG" + "AAA" * 60 + "TAG"  # 61 aa
        seq = short + "CCC" + long_
        assert _longest_orf_aa_length(seq) == 61

    def test_orf_on_reverse_strand_only(self):
        """Construct an ORF on the reverse strand and verify we find it."""
        from Bio.Seq import Seq
        forward_orf = "ATG" + "AAA" * 40 + "TAA"  # 41 aa on forward
        # Place it in a context and then reverse-complement the whole thing:
        # the ORF now exists only on the reverse strand of the final sequence.
        context = "GGG" * 10 + forward_orf + "CCC" * 10
        rc_context = str(Seq(context).reverse_complement())
        assert _longest_orf_aa_length(rc_context) == 41

    def test_premature_stop_in_frame_truncates_orf(self):
        """An in-frame stop between two ATGs must split into two shorter ORFs.

        Lengths are chosen above min_aa=30 so the threshold doesn't mask the
        split. The test fails if the implementation merges ORFs across a stop.
        """
        # M + 30K + stop  then  M + 50K + stop
        # Longest must be 51 aa, NOT 82 (merged) and NOT 31 (shorter one).
        seq = "ATG" + "AAA" * 30 + "TAA" + "ATG" + "AAA" * 50 + "TAA"
        assert _longest_orf_aa_length(seq) == 51


class TestORFLengthCrossValidation:
    """Cross-validate _longest_orf_aa_length against the independent
    _six_frame_orf_coords implementation (which uses Biopython's translate)."""

    def _longest_via_biopython_path(self, seq: str) -> int:
        orfs = _six_frame_orf_coords(seq, "test", min_aa=30)
        return max((o.aa_length for o in orfs), default=0)

    def test_random_sequences_agree(self):
        """On random DNA, both implementations must agree on the longest ORF."""
        random.seed(20260405)
        for _ in range(200):
            length = random.randint(60, 3000)
            seq = "".join(random.choices("ACGT", k=length))
            mine = _longest_orf_aa_length(seq)
            bio = self._longest_via_biopython_path(seq)
            assert mine == bio, (
                f"Divergence on seq of length {length}: "
                f"regex={mine}, biopython={bio}"
            )

    def test_random_sequences_with_n_agree(self):
        """Sequences containing N bases must still agree. N is never ATG/stop."""
        random.seed(20260406)
        for _ in range(100):
            length = random.randint(60, 2000)
            seq = "".join(random.choices("ACGTN", k=length))
            mine = _longest_orf_aa_length(seq)
            bio = self._longest_via_biopython_path(seq)
            assert mine == bio, f"Divergence on N-containing seq: {mine} vs {bio}"

    def test_find_longest_orf_length_matches_fast_path(self):
        """_find_longest_orf (full object) and _longest_orf_aa_length (fast)
        must agree on the aa length for every transcript."""
        random.seed(20260407)
        for _ in range(100):
            length = random.randint(90, 2000)
            seq = "".join(random.choices("ACGT", k=length))
            full = _find_longest_orf(seq, f"t{length}")
            fast = _longest_orf_aa_length(seq)
            full_len = full.aa_length if full else 0
            assert fast == full_len, (
                f"fast={fast}, full={full_len} on length-{length} seq"
            )


class TestDNALengthExact:
    """Length arithmetic must be exact. A bioinformatics tool with off-by-one
    length errors is one that silently corrupts coordinate-based analyses."""

    def test_transcript_length_equals_len_sequence(self):
        for seq in [
            "",
            "A",
            "ACGT",
            "A" * 1000,
            "ACGTN" * 500,
            "ATGAAATAG",
        ]:
            t = Transcript(id="x", description="", sequence=seq)
            assert t.length == len(seq) == len(t.sequence)

    def test_nucleotide_counts_sum_to_length(self):
        """For pure ACGTN sequences, the counts must sum exactly to length."""
        random.seed(42)
        for _ in range(50):
            length = random.randint(0, 5000)
            seq = "".join(random.choices("ACGTN", k=length))
            t = Transcript(id="x", description="", sequence=seq)
            counts = t.nucleotide_counts()
            assert sum(counts.values()) == t.length == length

    def test_gc_content_matches_manual_calculation(self):
        random.seed(43)
        for _ in range(50):
            length = random.randint(1, 2000)
            seq = "".join(random.choices("ACGT", k=length))
            t = Transcript(id="x", description="", sequence=seq)
            gc_manual = (seq.count("G") + seq.count("C")) / length * 100
            assert abs(t.gc_content - gc_manual) < 1e-9

    def test_gc_content_zero_for_empty(self):
        t = Transcript(id="x", description="", sequence="")
        assert t.gc_content == 0.0

    def test_gc_content_100_for_all_gc(self):
        t = Transcript(id="x", description="", sequence="GCGCGCGC")
        assert t.gc_content == 100.0

    def test_gc_content_0_for_all_at(self):
        t = Transcript(id="x", description="", sequence="ATATATAT")
        assert t.gc_content == 0.0


class TestFastaParsingExact:
    """FASTA parsing must never lose or duplicate a single base."""

    def test_single_line_sequence_preserved_exactly(self, tmp_path: Path):
        seq = "ACGT" * 250  # 1000 bp
        p = tmp_path / "single.fasta"
        p.write_text(f">test\n{seq}\n")
        [t] = load_all(str(p))
        assert t.sequence == seq
        assert t.length == 1000

    def test_multiline_sequence_joined_exactly(self, tmp_path: Path):
        """FASTA with 80-column line wrapping must reassemble to the exact
        original sequence — no dropped bases, no duplicated newlines."""
        full_seq = "".join(
            random.Random(7).choices("ACGT", k=5000)
        )
        wrapped = "\n".join(full_seq[i:i + 80] for i in range(0, len(full_seq), 80))
        p = tmp_path / "multi.fasta"
        p.write_text(f">test\n{wrapped}\n")
        [t] = load_all(str(p))
        assert t.sequence == full_seq
        assert t.length == 5000

    def test_varied_line_widths_preserved(self, tmp_path: Path):
        """Mixed line widths (60, 70, 80 chars) must still reassemble."""
        parts = ["A" * 60, "C" * 70, "G" * 80, "T" * 25]
        p = tmp_path / "varied.fasta"
        p.write_text(">test\n" + "\n".join(parts) + "\n")
        [t] = load_all(str(p))
        assert t.sequence == "".join(parts)
        assert t.length == 235

    def test_multiple_sequences_parsed_independently(self, tmp_path: Path):
        a = "ACGT" * 100
        b = "GGGG" * 50
        c = "NNNN" * 25
        p = tmp_path / "multi.fasta"
        p.write_text(f">a\n{a}\n>b\n{b}\n>c\n{c}\n")
        ts = load_all(str(p))
        assert len(ts) == 3
        assert ts[0].sequence == a and ts[0].length == 400
        assert ts[1].sequence == b and ts[1].length == 200
        assert ts[2].sequence == c and ts[2].length == 100

    def test_trailing_whitespace_stripped(self, tmp_path: Path):
        """Bases must survive trailing whitespace on sequence lines, but
        the whitespace itself must not be incorporated."""
        p = tmp_path / "ws.fasta"
        p.write_text(">test\nACGT   \nACGT\t\n")
        [t] = load_all(str(p))
        assert t.sequence == "ACGTACGT"
        assert t.length == 8

    def test_parse_fasta_bp_count_conserved(self, tmp_path: Path):
        """Build a FASTA with known total bp; parser must report the exact
        same number after loading."""
        random.seed(99)
        seqs = [
            "".join(random.choices("ACGT", k=random.randint(10, 500)))
            for _ in range(20)
        ]
        expected_total = sum(len(s) for s in seqs)
        p = tmp_path / "many.fasta"
        p.write_text("\n".join(f">t{i}\n{s}" for i, s in enumerate(seqs)) + "\n")
        ts = load_all(str(p))
        assert sum(t.length for t in ts) == expected_total
        for t, s in zip(ts, seqs):
            assert t.sequence == s

    def test_gzip_fasta_parsed_identically(self, tmp_path: Path):
        """A gzipped FASTA must parse to exactly the same transcripts as plain."""
        import gzip
        seq = "ACGT" * 500
        plain = tmp_path / "plain.fasta"
        gz = tmp_path / "gz.fasta.gz"
        plain.write_text(f">test\n{seq}\n")
        with gzip.open(gz, "wt") as f:
            f.write(f">test\n{seq}\n")
        [tp] = load_all(str(plain))
        [tg] = load_all(str(gz))
        assert tp.sequence == tg.sequence == seq
        assert tp.length == tg.length == 2000


class TestRoundTripLength:
    """Sanity: for every transcript we construct, every length path must agree."""

    def test_all_length_paths_agree(self):
        random.seed(777)
        for _ in range(50):
            length = random.randint(0, 3000)
            seq = "".join(random.choices("ACGTN", k=length))
            t = Transcript(id="x", description="", sequence=seq)
            # Dataclass field
            assert t.length == length
            # __post_init__ consistency
            assert t.length == len(t.sequence)
            # Counts sum (pure ACGTN)
            assert sum(t.nucleotide_counts().values()) == length
