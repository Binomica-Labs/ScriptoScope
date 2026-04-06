"""Tests for Prodigal integration — GFF3 parsing, gene prediction, persistence.

Run with:  pytest tests/test_prodigal.py -v
"""
from __future__ import annotations

import json
import tempfile
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scriptoscope import (
    ProdigalGene,
    parse_prodigal_gff,
    prodigal_available,
    suggest_bacterial_mode,
    save_annotations,
    load_annotations,
    Transcript,
)


# ══════════════════════════════════════════════════════════════════════════════
# Sample Prodigal GFF3 + protein FASTA data
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_GFF = """\
##gff-version  3
# Sequence Data: seqnum=1;seqlen=3000;seqhdr="transcript_1"
# Model Data: version=Prodigal.v2.6.3;run_type=Metagenomic;model=1
transcript_1\tProdigal_v2.6.3\tCDS\t101\t835\t52.3\t+\t0\tID=1_1;partial=00;start_type=ATG;rbs_motif=None;rbs_spacer=None;gc_cont=0.450;conf=99.99;score=52.3;cscore=48.21;sscore=4.09;rscore=-2.30;uscore=3.70;tscore=2.69
transcript_1\tProdigal_v2.6.3\tCDS\t838\t1378\t41.7\t+\t0\tID=1_2;partial=00;start_type=ATG;rbs_motif=None;rbs_spacer=None;gc_cont=0.430;conf=99.50;score=41.7;cscore=38.10;sscore=3.60;rscore=-1.20;uscore=2.50;tscore=2.30
transcript_1\tProdigal_v2.6.3\tCDS\t1380\t2316\t67.8\t-\t0\tID=1_3;partial=01;start_type=GTG;rbs_motif=None;rbs_spacer=None;gc_cont=0.480;conf=99.99;score=67.8;cscore=62.50;sscore=5.30;rscore=-0.80;uscore=3.10;tscore=3.00
"""

SAMPLE_PROTEINS = """\
>transcript_1_1_1 # 101 # 835 # 1 # ID=1_1;partial=00;start_type=ATG
MKVLGIDGGSSEKTTLVRAQFTGAIPELDLDIGEISDGDLAR
IKNDPNDQTTSVANKQGYEWALNFQTNAALKQHPELIRRVNA
FIAELGIPLAYILAHDWGHAQISLLAQAQNAPKIHIDATKLAG
TRDALLEAAIADAENMLTIIEPIQGEEGGILAESMGIFDSRVL
ALAGLSGYPNVAYIEGHGQTTNKLIDQVFAAIRDLLSKNIDV
VFTGKVAIIGAGDI*
>transcript_1_1_2 # 838 # 1378 # 1 # ID=1_2;partial=00;start_type=ATG
MSERIDQALAELQPHGADLVHNTFISHTANEKDRKVLNTKLN
NDYRSELSTRSALYEFNLPGEQNSPYFTALNKSLDQLAPHRH
GQPENMDTIAQLATAKPALEKVLQDRWDTPQGIEQIVEMFAG
AVKQVNGTGDIVYLKDTLISNPEGKATEKYLTHVFEEFLTRC
VEESGA*
>transcript_1_1_3 # 1380 # 2316 # -1 # ID=1_3;partial=01;start_type=GTG
VKVLGIDGGSSEKTTLVRAQFTGAIPELDLDIGEISDGDLAR
IKNDPNDQTTSVANKQGYEWALNFQTNAALKQHPELIRRVNA
FIAELGIPLAYILAHDWGHAQISLLAQAQNAPKIHIDATKLAG
TRDALLEAAIADAENMLTIIEPIQGEEGGILAESMGIFDSRVL
ALAGLSGYPNVAYIEGHGQTTNKLIDQVFAAIRDLLSKNIDV
VFTGKVAIIGAGDIVNHIPAAGTQKENYTFDIDAIFKAGAAG
TATHVKAVADALMENLRQRIGEAHKGEIVPGGVLTETHLA*
"""


class TestParseProdigalGFF:
    """Test GFF3 + protein FASTA parsing."""

    def test_basic_parsing(self):
        result = parse_prodigal_gff(SAMPLE_GFF, SAMPLE_PROTEINS)
        assert "transcript_1" in result
        genes = result["transcript_1"]
        assert len(genes) == 3

    def test_gene1_coordinates(self):
        result = parse_prodigal_gff(SAMPLE_GFF, SAMPLE_PROTEINS)
        gene1 = result["transcript_1"][0]
        # GFF is 1-based: start=101 -> 0-based=100, end=835 -> 0-based exclusive=835
        assert gene1.start == 100
        assert gene1.end == 835
        assert gene1.strand == "+"
        assert gene1.partial == "00"
        assert gene1.score == pytest.approx(52.3)

    def test_gene3_minus_strand(self):
        result = parse_prodigal_gff(SAMPLE_GFF, SAMPLE_PROTEINS)
        gene3 = result["transcript_1"][2]
        assert gene3.strand == "-"
        assert gene3.partial == "01"  # no stop
        assert gene3.score == pytest.approx(67.8)

    def test_protein_sequences_stripped(self):
        """Protein sequences should have trailing * (stop) stripped."""
        result = parse_prodigal_gff(SAMPLE_GFF, SAMPLE_PROTEINS)
        for gene in result["transcript_1"]:
            assert not gene.aa_sequence.endswith("*"), (
                f"Gene {gene.gene_id} AA sequence should not end with '*'"
            )

    def test_protein_sequences_nonempty(self):
        result = parse_prodigal_gff(SAMPLE_GFF, SAMPLE_PROTEINS)
        for gene in result["transcript_1"]:
            assert len(gene.aa_sequence) > 0

    def test_gene_ids(self):
        result = parse_prodigal_gff(SAMPLE_GFF, SAMPLE_PROTEINS)
        genes = result["transcript_1"]
        assert genes[0].gene_id == "transcript_1_1_1"
        assert genes[1].gene_id == "transcript_1_1_2"
        assert genes[2].gene_id == "transcript_1_1_3"

    def test_empty_gff(self):
        result = parse_prodigal_gff("", "")
        assert result == {}

    def test_comment_only_gff(self):
        gff = "##gff-version  3\n# comment line\n"
        result = parse_prodigal_gff(gff, "")
        assert result == {}

    def test_no_protein_match(self):
        """Genes with no matching protein should have empty aa_sequence."""
        gff = (
            "seq1\tProdigal_v2.6.3\tCDS\t1\t300\t10.0\t+\t0\t"
            "ID=1;partial=00;start_type=ATG\n"
        )
        result = parse_prodigal_gff(gff, "")
        assert len(result["seq1"]) == 1
        assert result["seq1"][0].aa_sequence == ""

    def test_multiple_sequences(self):
        """Parse GFF with genes from multiple sequences."""
        gff = (
            "seq1\tProdigal_v2.6.3\tCDS\t1\t300\t10.0\t+\t0\t"
            "ID=1_1;partial=00\n"
            "seq2\tProdigal_v2.6.3\tCDS\t50\t400\t20.0\t-\t0\t"
            "ID=2_1;partial=10\n"
        )
        result = parse_prodigal_gff(gff, "")
        assert "seq1" in result
        assert "seq2" in result
        assert len(result["seq1"]) == 1
        assert len(result["seq2"]) == 1
        assert result["seq2"][0].partial == "10"


class TestSuggestBacterialMode:
    """Test the bacterial mode auto-detection heuristic."""

    def test_empty_list(self):
        assert suggest_bacterial_mode([]) is False

    def test_short_transcripts(self):
        """Short transcripts should not suggest bacterial mode."""
        transcripts = [
            Transcript(id=f"t{i}", description="", sequence="ATGC" * 100)
            for i in range(10)
        ]
        assert suggest_bacterial_mode(transcripts) is False

    def test_single_transcript_too_few(self):
        """A single transcript isn't enough evidence."""
        t = Transcript(id="t1", description="", sequence="ATGC" * 1000)
        assert suggest_bacterial_mode([t]) is False


class TestProdigalPersistence:
    """Test save/load of Prodigal cache in annotations sidecar."""

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fasta_path = os.path.join(tmpdir, "test.fasta")
            # Create a dummy FASTA
            with open(fasta_path, "w") as f:
                f.write(">seq1\nATGCATGC\n")

            genes = [
                ProdigalGene(
                    gene_id="seq1_1",
                    start=0,
                    end=300,
                    strand="+",
                    partial="00",
                    score=42.5,
                    aa_sequence="MKVLGIDG",
                ),
                ProdigalGene(
                    gene_id="seq1_2",
                    start=310,
                    end=600,
                    strand="-",
                    partial="01",
                    score=30.1,
                    aa_sequence="MSERIDQA",
                ),
            ]
            prodigal_cache = {"seq1": genes}

            save_annotations(
                fasta_path=fasta_path,
                scan_cache={},
                confirm_cache={},
                pfam_hits={},
                bookmarks=set(),
                orf_cache={},
                predictions=None,
                prodigal_cache=prodigal_cache,
            )

            loaded = load_annotations(fasta_path)
            assert loaded is not None
            assert "prodigal_cache" in loaded
            loaded_genes = loaded["prodigal_cache"].get("seq1", [])
            assert len(loaded_genes) == 2
            assert loaded_genes[0].gene_id == "seq1_1"
            assert loaded_genes[0].start == 0
            assert loaded_genes[0].end == 300
            assert loaded_genes[0].strand == "+"
            assert loaded_genes[0].partial == "00"
            assert loaded_genes[0].score == pytest.approx(42.5)
            assert loaded_genes[0].aa_sequence == "MKVLGIDG"
            assert loaded_genes[1].gene_id == "seq1_2"
            assert loaded_genes[1].strand == "-"
            assert loaded_genes[1].partial == "01"

    def test_save_load_empty_prodigal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fasta_path = os.path.join(tmpdir, "test.fasta")
            with open(fasta_path, "w") as f:
                f.write(">seq1\nATGCATGC\n")

            save_annotations(
                fasta_path=fasta_path,
                scan_cache={},
                confirm_cache={},
                pfam_hits={},
                bookmarks=set(),
                orf_cache={},
                predictions=None,
                prodigal_cache=None,
            )

            loaded = load_annotations(fasta_path)
            assert loaded is not None
            assert loaded.get("prodigal_cache") == {}


class TestProdigalAvailable:
    """Test prodigal_available helper."""

    def test_returns_bool(self):
        result = prodigal_available()
        assert isinstance(result, bool)


class TestProdigalGeneDataclass:
    """Test ProdigalGene dataclass basics."""

    def test_create(self):
        g = ProdigalGene(
            gene_id="test_1",
            start=0,
            end=300,
            strand="+",
            partial="00",
            score=50.0,
            aa_sequence="MKVL",
        )
        assert g.gene_id == "test_1"
        assert g.start == 0
        assert g.end == 300
        assert g.strand == "+"
        assert g.aa_sequence == "MKVL"

    def test_partial_codes(self):
        """Verify all partial code combos are accepted."""
        for code in ("00", "10", "01", "11"):
            g = ProdigalGene(
                gene_id="t", start=0, end=100,
                strand="+", partial=code, score=0.0, aa_sequence="",
            )
            assert g.partial == code
