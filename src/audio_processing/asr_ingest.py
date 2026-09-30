"""Reactor-side ASR ingest bookkeeping (pure Python; no Twisted, no models).

The audio protocol hands every resampled mono chunk to the ASR thread through
a bounded queue. The reactor must never wait on that queue, so chunks can be
dropped when the consumer stalls; three small pieces make a drop harmless and
visible, and they live here so they can be unit-tested without a reactor:

  * StampedChunk — PCM bytes carrying the absolute offset (in samples) of
    their first sample, counted over ALL audio received on the connection.
    Window timestamps derive from this rather than from what survived the
    queue, so they stay on the same clock as AudioBuffer (which keeps every
    byte) and ``extract()`` returns the audio the words were spoken in.
  * IngestCounter — received/dropped totals plus the rate-limited WARNING.
  * WindowAssembler — turns stamped chunks into fixed-length windows, zero-
    filling a short gap left by dropped chunks and restarting on a long one.
"""
import time

SAMPLE_RATE = 16000
DEPTH = 2  # bytes per sample (16-bit PCM)

# Live chunks are ~85 ms (4096 float32 samples at 48 kHz -> ~2.7 KB mono
# int16), so this is ~20 s of slack before anything is lost. The old maxsize=3
# was a quarter of a second — one GIL hiccup away from evicting audio.
QUEUE_CHUNKS = 256


class StampedChunk(bytes):
    """PCM bytes plus the absolute sample offset of their first sample.

    A ``bytes`` subclass on purpose: consumers that only join and measure
    chunks (GoogleASR) keep working unchanged, and the offset rides along for
    consumers that need the clock (CrisperWhisper windows).
    """

    def __new__(cls, data, sample_offset):
        self = super().__new__(cls, data)
        self.sample_offset = int(sample_offset)
        return self


class IngestCounter:
    """Per-connection received/dropped accounting for the ASR ingest queue.

    ``received`` returns the sample offset to stamp the chunk with; ``dropped``
    records a loss and returns the WARNING text when one is due — the first
    drop immediately, then at most once per ``log_interval`` seconds with the
    running totals, so a stalled consumer can't flood the log.
    """

    def __init__(self, sample_rate=SAMPLE_RATE, depth=DEPTH, log_interval=30.0):
        self.sample_rate = sample_rate
        self.depth = depth
        self.log_interval = log_interval
        self.samples_received = 0
        self.chunks_received = 0
        self.samples_dropped = 0
        self.chunks_dropped = 0
        self._since_log_chunks = 0
        self._since_log_samples = 0
        self._last_log = None

    def received(self, nbytes):
        offset = self.samples_received
        self.samples_received += nbytes // self.depth
        self.chunks_received += 1
        return offset

    def seconds(self, samples):
        return samples / float(self.sample_rate)

    def dropped(self, nbytes, now=None):
        now = time.time() if now is None else now
        samples = nbytes // self.depth
        self.samples_dropped += samples
        self.chunks_dropped += 1
        self._since_log_chunks += 1
        self._since_log_samples += samples
        if self._last_log is not None and now - self._last_log < self.log_interval:
            return None
        text = ("ASR ingest queue full: dropped %d chunks (%.1fs) since last report; "
                "%d of %d chunks (%.1fs of %.1fs) dropped on this connection" % (
                    self._since_log_chunks, self.seconds(self._since_log_samples),
                    self.chunks_dropped, self.chunks_received,
                    self.seconds(self.samples_dropped), self.seconds(self.samples_received)))
        self._last_log = now
        self._since_log_chunks = 0
        self._since_log_samples = 0
        return text


class WindowAssembler:
    """Assemble stamped chunks into windows of at least ``window_seconds``.

    Each window is ``(start_seconds, pcm_bytes)`` where ``start_seconds`` is
    the absolute offset of the window's first sample. A gap shorter than a
    window (a few dropped chunks) is zero-filled so the audio stays on the
    clock; a longer gap closes the current window and starts a new one at the
    new offset. Unstamped ``bytes`` are assumed contiguous with the previous
    chunk.
    """

    def __init__(self, sample_rate=SAMPLE_RATE, depth=DEPTH, window_seconds=12.0):
        self.sample_rate = sample_rate
        self.depth = depth
        self.window_samples = int(window_seconds * sample_rate)
        self._buffer = bytearray()
        self._start = None  # sample offset of self._buffer[0]

    def _expected(self):
        return (self._start or 0) + len(self._buffer) // self.depth

    def add(self, chunk):
        offset = getattr(chunk, "sample_offset", None)
        windows = []
        if self._start is None:
            self._start = self._expected() if offset is None else offset
        elif offset is not None:
            gap = offset - self._expected()
            if gap >= self.window_samples:
                tail = self.flush()
                if tail:
                    windows.append(tail)
                self._start = offset
            elif gap > 0:
                self._buffer.extend(bytes(gap * self.depth))
        self._buffer.extend(chunk)
        if len(self._buffer) // self.depth >= self.window_samples:
            windows.append(self.flush())
        return windows

    def flush(self):
        """Emit whatever is buffered (the final partial window), or None."""
        if len(self._buffer) < self.depth:
            return None
        window = (self._start / float(self.sample_rate), bytes(self._buffer))
        self._start += len(self._buffer) // self.depth
        self._buffer = bytearray()
        return window
