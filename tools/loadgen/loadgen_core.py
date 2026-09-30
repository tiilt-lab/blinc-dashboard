"""Pure parts of the BLINC live-audio load generator: no network, no threads.

Everything here mirrors either the browser BYOD client
(frontend/src/byod-join/byod-join-component.jsx, public/audio-sender-processor.js,
frontend/src/globals.js) or the audio service (src/audio_processing/server.py,
src/common/processing_config_base.py), with the source cited as file:line so
tests/test_loadgen.py can pin the protocol without opening a socket.
"""
import math
import random
import re
from datetime import datetime

# --- audio format, exactly what the browser sends -----------------------------
SAMPLE_RATE = 16000          # byod-join-component.jsx:1635  new AudioContext({sampleRate: 16000})
CHUNK_SAMPLES = 4096         # public/audio-sender-processor.js:2  bufferSize = 4096
BYTES_PER_SAMPLE = 4         # Float32Array -> "pcm_f32le" (byod-join-component.jsx:2155)
CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE          # 16384 bytes per binary frame
CHUNK_SECONDS = CHUNK_SAMPLES / SAMPLE_RATE             # 0.256 s, ~3.9 frames/s

# --- reconnect policy, byod-join-component.jsx:24-36 --------------------------
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 30.0
RECONNECT_DEADLINE_S = 180.0
RECONNECT_ATTEMPT_TIMEOUT_S = 25.0
AUDIO_BACKLOG_CAP_BYTES = 12 * 1024 * 1024

MAX_PODS = 32
# Names: tables/session.py:25-26 and tables/session_device.py:39-40 allow only
# these characters (no '-' or '_'), at most 64 of them.
NAME_RE = re.compile(r"^[a-zA-Z0-9': ]{1,64}\Z")
SESSION_PREFIX = "LOADTEST"

# WARNING texts introduced by Phase 1 for dropped audio/windows:
DROP_WARNINGS = (
    "ASR ingest queue full: dropped",                 # asr_ingest.py:83
    "CrisperWhisper: transcription backlog full",      # asr_connectors/crisperwhisper_asr.py:442
    "CrisperWhisper: dropped the window at",           # asr_connectors/crisperwhisper_asr.py:449
)


# --- protocol messages --------------------------------------------------------
def start_message(key, device_id, session_id, embeddings=None, start_time=0.0):
    """The browser's audio `start`, field for field (byod-join-component.jsx:2148-2162).

    The service validates key/sample_rate/encoding/channels/sessionid/deviceid
    (processing_config_base.py:22-49) and looks the key up in Redis
    (processing_config.py:60); everything else is optional. numSpeakers=0 is
    the "speakers added after the session starts" path (server.py:104-107).
    """
    return {
        "type": "start",
        "key": key,
        "start_time": start_time,
        "sample_rate": SAMPLE_RATE,
        "encoding": "pcm_f32le",
        "channels": 1,
        "streamdata": "audio",
        "tag": True,
        "embeddings_file": embeddings,
        "deviceid": device_id,
        "sessionid": session_id,
        "numSpeakers": 0,
    }


def speaker_done_message(speakers=()):
    """Roster confirmation sent right behind `start` (byod-join-component.jsx:1387-1392).

    Not optional: the service ignores binary audio until this clears
    awaitingSpeakers (server.py:80, :105, :224).
    """
    return {"type": "speaker", "id": "done", "speakers": list(speakers)}


def backoff_delay(attempt, base=RECONNECT_BASE_S, cap=RECONNECT_MAX_S, rand=random.random):
    """globals.js:166-170 backoffDelay: exponential with full jitter, in seconds."""
    ceiling = min(cap, base * 2 ** max(0, attempt or 0))
    floor = min(ceiling, base / 2)
    return floor + rand() * (ceiling - floor)


def backlog_seconds(nbytes):
    """audioBacklogSeconds (byod-join-component.jsx:965-969): start_time on a re-sent start."""
    return round(nbytes / (BYTES_PER_SAMPLE * SAMPLE_RATE), 2)


# --- chunking and cadence -----------------------------------------------------
def total_samples(pcm):
    return len(pcm) // BYTES_PER_SAMPLE


def chunk_at(pcm, index, start_sample=0):
    """Frame `index` of a pod that began `start_sample` samples into the looped recording.

    Always exactly CHUNK_BYTES (the worklet only flushes full buffers while
    running, audio-sender-processor.js:19-21); wraps around the end.
    """
    total = total_samples(pcm)
    if total < CHUNK_SAMPLES:
        raise ValueError("recording shorter than one chunk")
    pos = (start_sample + index * CHUNK_SAMPLES) % total
    out = pcm[pos * BYTES_PER_SAMPLE:pos * BYTES_PER_SAMPLE + CHUNK_BYTES]
    while len(out) < CHUNK_BYTES:
        out += pcm[:CHUNK_BYTES - len(out)]
    return out


def chunk_deadline(t0, index):
    """Absolute send time of frame `index`: no drift, N pods are N times real load."""
    return t0 + index * CHUNK_SECONDS


def pod_start_samples(n_pods, total):
    """Spread pod start offsets over the recording so ASR windows are not in phase."""
    return [int(i * total / n_pods) for i in range(n_pods)]


# --- plan ---------------------------------------------------------------------
def parse_ramp(text):
    steps = [int(x) for x in str(text).split(",") if x.strip()]
    if not steps or any(s <= 0 for s in steps):
        raise ValueError("ramp needs positive pod counts, e.g. 2,4,8")
    if any(b <= a for a, b in zip(steps, steps[1:])):
        raise ValueError("ramp must be strictly increasing")
    if steps[-1] > MAX_PODS:
        raise ValueError("at most %d pods" % MAX_PODS)
    return steps


def ramp_plan(steps, step_seconds):
    """[(step_no, pods_total, new_pods, start_s, end_s)] — each step ADDS pods."""
    plan, previous, t = [], 0, 0.0
    for i, pods in enumerate(steps, 1):
        plan.append((i, pods, pods - previous, t, t + step_seconds))
        previous, t = pods, t + step_seconds
    return plan


def session_name_ok(name):
    return name.startswith(SESSION_PREFIX) and bool(NAME_RE.match(name))


def default_session_name(now):
    return "%s %s" % (SESSION_PREFIX, now.strftime("%Y%m%d %H%M%S"))


def pod_alias(n):
    return "loadgen%d" % n


# --- the session clock --------------------------------------------------------
def parse_creation_date(text):
    """session.json() creation_date, 'YYYY-MM-DD HH:MM:SS[.ffffff] UTC' (tables/session.py:46)."""
    text = text.strip()
    if text.endswith(" UTC"):
        text = text[:-4]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError("unrecognised creation_date %r" % text)


def server_start_offset(start_sent_utc, creation_date, offset=0.0):
    """processing_config.py:52: seconds from session creation to this stream's first sample.

    The service reads an `offset` field (processing_config_base.py:32); the
    browser sends `start_time` instead, so the service sees 0 — pass 0 here to
    match what it actually does.
    """
    return max((start_sent_utc - creation_date).total_seconds() - offset, 0.0)


def transcript_rows(payload):
    """Rows of /transcriptspeakermetrics/client are {transcript, speaker_metrics}
    (routes/session.py:768-771); accept bare transcripts too."""
    for row in payload or []:
        if isinstance(row, dict):
            yield row.get("transcript", row)


def latest_end(rows, current=None):
    """max(start_time + length): integer seconds on the session clock (tables/transcript.py:7-8)."""
    for t in rows:
        try:
            end = float(t["start_time"]) + float(t["length"])
        except (KeyError, TypeError, ValueError):
            continue
        current = end if current is None else max(current, end)
    return current


def max_id(rows, current=0):
    for t in rows:
        try:
            current = max(current, int(t["id"]))
        except (KeyError, TypeError, ValueError):
            pass
    return current


def caption_lag(audio_clock_s, latest_end_s, start_offset_s):
    """Seconds between the audio already sent and the latest caption, on the session clock.

    audio_clock_s = start_offset + seconds sent on this connection (where the
    service places the last sample); with no caption yet the reference is the
    stream's first sample, so the lag is simply the audio sent so far.
    """
    reference = start_offset_s if latest_end_s is None else latest_end_s
    return max(audio_clock_s - reference, 0.0)


# --- host log -----------------------------------------------------------------
def count_drop_warnings(text):
    return sum(1 for line in text.splitlines()
               if "WARNING" in line and any(m in line for m in DROP_WARNINGS))


# --- summary and verdict ------------------------------------------------------
def percentile(values, p):
    """Nearest-rank percentile; None for no values."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[max(0, min(len(values) - 1, math.ceil(p / 100.0 * len(values)) - 1))]


def _f(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def summarize(rows, max_lag_s, warmup_s=0.0):
    """Per-step summary of sample rows plus the verdict.

    rows: dicts (CSV rows are fine) with step, pods_target, step_elapsed_s, ts,
    pod, lag_s, asr_state, degraded_events, drop_warnings, gpu_util,
    gpu_mem_mb, load1. A step passes when the p95 lag across pods stayed under
    max_lag_s at EVERY tick after the warm-up.
    """
    steps = {}
    for r in rows:
        steps.setdefault(int(r["step"]), []).append(r)
    out = []
    for step in sorted(steps):
        all_rows = steps[step]
        rows_ = [r for r in all_rows if (_f(r, "step_elapsed_s") or 0.0) >= warmup_s]
        ticks = {}
        for r in rows_:
            ticks.setdefault(r["ts"], []).append(_f(r, "lag_s"))
        tick_p95 = [percentile(v, 95) for v in ticks.values()]
        lags = [_f(r, "lag_s") for r in rows_]
        drops = [_f(r, "drop_warnings") for r in all_rows if _f(r, "drop_warnings") is not None]
        gpu = [_f(r, "gpu_util") for r in rows_ if _f(r, "gpu_util") is not None]
        out.append({
            "step": step,
            "pods": int(all_rows[0]["pods_target"]),
            "ticks": len(ticks),
            "median_lag_s": percentile(lags, 50),
            "p95_lag_s": percentile(lags, 95),
            "max_lag_s": max((l for l in lags if l is not None), default=None),
            "worst_tick_p95_s": max((p for p in tick_p95 if p is not None), default=None),
            "degraded_pods": sorted({r["pod"] for r in all_rows
                                     if r.get("asr_state") == "degraded" or (_f(r, "degraded_events") or 0) > 0}),
            "drop_warnings": int(max(drops) - min(drops)) if drops else 0,
            "gpu_util_mean": (sum(gpu) / len(gpu)) if gpu else None,
            "gpu_util_max": max(gpu) if gpu else None,
            "gpu_mem_mb_max": max((_f(r, "gpu_mem_mb") for r in rows_ if _f(r, "gpu_mem_mb") is not None), default=None),
            "load1_max": max((_f(r, "load1") for r in rows_ if _f(r, "load1") is not None), default=None),
            "passed": bool(tick_p95) and all(p is not None and p < max_lag_s for p in tick_p95),
        })
    passed = [s["pods"] for s in out if s["passed"]]
    verdict = {
        "max_lag_s": max_lag_s,
        "warmup_s": warmup_s,
        "max_pods_under_lag": max(passed) if passed else 0,
        "text": ("VERDICT: %d simultaneous pods kept p95 caption lag under %.0f s for a whole step"
                 % (max(passed), max_lag_s)) if passed else
                ("VERDICT: no step kept p95 caption lag under %.0f s" % max_lag_s),
    }
    return out, verdict
