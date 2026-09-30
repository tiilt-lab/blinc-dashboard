"""Source-level contracts for the Phase 1 audio changes.

server.py loads models at import and callbacks.py needs requests, so these
read the source (same style as test_sweep_contracts.py) to keep the fixes
from quietly regressing.
"""
import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing")


def _read(*parts):
    with open(os.path.join(SRC, *parts)) as f:
        return f.read()


def test_reactor_never_blocks_on_the_asr_ingest_queue():
    s = _read("server.py")
    assert "self.asr_audio_queue.put(" not in s, "blocking put on the reactor is back"
    assert "timeout=0.05" not in s
    assert "self.asr_audio_queue.put_nowait(" in s
    assert "asr_ingest.StampedChunk(" in s and "asr_ingest.IngestCounter()" in s
    assert "maxsize=3)" not in s


def test_audio_factory_has_keepalive_and_a_payload_cap():
    s = _read("server.py")
    m = re.search(r"factory\.setProtocolOptions\((.*?)\)\n", s, re.DOTALL)
    assert m, "audio WebSocketServerFactory must call setProtocolOptions"
    opts = m.group(1)
    assert "autoPingInterval=10" in opts and "autoPingTimeout=20" in opts
    cap = re.search(r"maxMessagePayloadSize=([\d\s*]+)", opts)
    assert cap and eval(cap.group(1)) >= 32 * 1024 * 1024, \
        "enrollment sends a ~23.5 MB webm blob as one message"


def test_asr_status_message_shape():
    s = _read("server.py")
    assert "{'type': 'asr_status', 'state': state}" in s
    assert "self.asr.on_status = self._send_asr_status" in s
    cw = _read("asr_connectors", "crisperwhisper_asr.py")
    assert 'self._set_state("ok")' in cw and '"degraded"' in cw


def test_worker_spawn_is_breakered_and_stderr_is_kept():
    cw = _read("asr_connectors", "crisperwhisper_asr.py")
    assert "stderr=subprocess.DEVNULL" not in cw
    assert "class SpawnBackoff" in cw and "MIN_FREE_VRAM_MIB = 3500" in cw
    assert "raise WorkerUnavailable" in cw
    assert "for attempt in (1, 2)" not in cw, "the respawn-and-retry-once loop must stay gone"


def test_callbacks_retry_with_idempotency_keys_and_keep_the_timeout():
    s = _read("callbacks.py")
    assert "callback_retry.RetryQueue(" in s
    for kind in ("transcript", "metrics", "connect"):
        assert "_post_with_retry('%s'" % kind in s, kind
    assert "timeout=callbacks_common.CALLBACK_TIMEOUT" in s
    assert "callbacks_common.post_connect(" not in s, "connect must go through the retry queue"


def test_speaker_metrics_post_is_outside_the_process_lock():
    lines = _read("speaker_metrics", "speaker_metrics.py").split("\n")
    with_line = next(i for i, l in enumerate(lines) if l.strip() == "with self.process_lock:")
    post_line = next(i for i, l in enumerate(lines)
                     if "callbacks.post_speaker_transcript_metrics(" in l)
    indent = len(lines[with_line]) - len(lines[with_line].lstrip())
    post_indent = len(lines[post_line]) - len(lines[post_line].lstrip())
    assert post_line > with_line and post_indent <= indent, \
        "the HTTP post must not run under process_lock"


def test_processor_guards_each_optional_stage_and_still_posts():
    s = _read("processor.py")
    for stage in ("questions", "keywords", "features", "topic", "doa",
                  "fingerprint match", "speaker embedding"):
        assert re.search(r"self\._optional\(\s*'%s'" % stage, s), stage
    assert "callbacks.post_transcripts(" in s
