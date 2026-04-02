
import asyncio
from pathlib import Path
from scriptoscope import Transcript, _compute_stats, colorize_sequence, _parse_fasta

def test_stats():
    # Create some dummy transcripts
    t1 = Transcript(id="seq1", description="desc1", sequence="ATGC" * 25) # 100bp, 50% GC
    t2 = Transcript(id="seq2", description="desc2", sequence="ATGC" * 50) # 200bp, 50% GC
    transcripts = [t1, t2]
    
    stats = _compute_stats(transcripts)
    print("Stats:", stats)
    assert stats['n'] == 2
    assert stats['total_bases'] == 300
    assert stats['shortest'] == 100
    assert stats['longest'] == 200

def test_colorize():
    t = Transcript(id="seq1", description="desc1", sequence="ATGC" * 10)
    colored = colorize_sequence(t.sequence)
    print("Colored sequence length:", len(colored))
    assert len(colored) > 0

if __name__ == "__main__":
    test_stats()
    test_colorize()
    print("Basic logic tests passed.")
