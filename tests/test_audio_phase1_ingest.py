"""ASR ingest bookkeeping (src/audio_processing/asr_ingest.py) — pure, no Twisted.

Audit B.1: the reactor used to block on put(timeout=0.05) into a 3-deep queue,
and window timestamps advanced only by KEPT audio, so one drop shifted every
later transcript against AudioBuffer (which keeps everything). Chunks now
carry their absolute sample offset, the assembler derives window start times
from it, and drops are counted and reported at a bounded rate.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

from asr_ingest import StampedChunk, IngestCounter, WindowAssembler  # noqa: E402

SR = 16000


def pcm(seconds, value=1):
    return value.to_bytes(2, "little", signed=True) * int(seconds * SR)


def test_stamped_chunk_is_bytes_compatible():
    # GoogleASR only joins and measures chunks; it must keep working unchanged.
    c = StampedChunk(bytearray(b"\x01\x00" * 4), 320)
    assert isinstance(c, bytes) and len(c) == 8 and c.sample_offset == 320
    assert b"".join([c, c]) == bytes(c) * 2


def test_counter_offsets_count_all_received_audio_including_drops():
    n = IngestCounter()
    assert n.received(3200) == 0        # 1600 samples
    assert n.received(3200) == 1600
    n.dropped(3200, now=0.0)            # a drop never rewinds the clock
    assert n.received(3200) == 3200
    assert n.samples_dropped == 1600 and n.chunks_dropped == 1
    assert n.seconds(n.samples_received) == 0.3


def test_counter_warning_is_rate_limited_and_carries_totals():
    n = IngestCounter(log_interval=30)
    for _ in range(4):
        n.received(3200)
    first = n.dropped(3200, now=100.0)
    assert first and "dropped 1 chunks (0.1s)" in first and "1 of 4 chunks" in first
    assert n.dropped(3200, now=110.0) is None
    assert n.dropped(3200, now=129.9) is None
    again = n.dropped(3200, now=131.0)
    assert again and "dropped 3 chunks (0.3s)" in again and "4 of 4 chunks" in again


def test_assembler_window_starts_follow_absolute_offsets():
    a = WindowAssembler(SR, 2, window_seconds=1.0)
    out = []
    for i in range(4):  # contiguous 0.5 s chunks
        out += a.add(StampedChunk(pcm(0.5), i * 8000))
    assert [w[0] for w in out] == [0.0, 1.0]
    assert all(len(w[1]) == 32000 for w in out)


def test_assembler_zero_fills_a_short_gap_so_words_stay_on_the_clock():
    a = WindowAssembler(SR, 2, window_seconds=1.0)
    a.add(StampedChunk(pcm(0.25), 0))                     # samples 0..4000
    assert a.add(StampedChunk(pcm(0.25), 10000)) == []    # 6000 samples lost; 14000 < window
    (start, data), = a.add(StampedChunk(pcm(0.25), 14000))  # contiguous; 18000 >= window
    assert start == 0.0 and len(data) == 18000 * 2
    assert data[8000:20000] == bytes(12000)               # the gap is silence, on the clock
    assert data[20000:] == pcm(0.5)


def test_assembler_restarts_after_a_long_gap_instead_of_fabricating_silence():
    a = WindowAssembler(SR, 2, window_seconds=1.0)
    a.add(StampedChunk(pcm(0.5), 0))
    out = a.add(StampedChunk(pcm(0.5), 10 * SR))          # 9.5 s missing
    assert [(s, len(d)) for s, d in out] == [(0.0, 16000)]
    (start, data), = a.add(StampedChunk(pcm(0.5), 10 * SR + 8000))
    assert start == 10.0 and len(data) == 32000


def test_assembler_flush_returns_the_partial_window_then_none():
    a = WindowAssembler(SR, 2, window_seconds=1.0)
    assert a.flush() is None
    a.add(StampedChunk(pcm(0.5), 32000))
    assert a.flush() == (2.0, pcm(0.5))
    assert a.flush() is None


def test_unstamped_bytes_are_treated_as_contiguous():
    a = WindowAssembler(SR, 2, window_seconds=1.0)
    a.add(pcm(0.5))
    (start, data), = a.add(pcm(0.5))
    assert start == 0.0 and len(data) == 32000
