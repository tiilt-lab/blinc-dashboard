"""Unit tests for the pure parts of tools/loadgen (no network, no websockets).

loadgen_core mirrors the BYOD client and the audio service; these pin the
chunk math, the start message, the lag definition, the ramp plan and the
verdict so a protocol drift shows up here before it shows up on the box.
"""
import os
import shutil
import subprocess
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "loadgen"))

import loadgen_core as core  # noqa: E402


# --- chunking and cadence -----------------------------------------------------
def test_chunk_matches_the_audio_worklet_frame():
    # audio-sender-processor.js: Float32Array(4096) at the 16 kHz AudioContext
    assert core.CHUNK_BYTES == 4096 * 4 == 16384
    assert core.CHUNK_SECONDS == pytest.approx(0.256)
    assert core.chunk_deadline(100.0, 10) == pytest.approx(102.56)


def test_chunk_at_loops_the_recording_and_is_always_full_size():
    total = core.CHUNK_SAMPLES * 2 + 100          # not a multiple of the chunk
    pcm = bytes(range(256)) * (total * core.BYTES_PER_SAMPLE // 256 + 1)
    pcm = pcm[:total * core.BYTES_PER_SAMPLE]
    first = core.chunk_at(pcm, 0)
    assert len(first) == core.CHUNK_BYTES and first == pcm[:core.CHUNK_BYTES]
    third = core.chunk_at(pcm, 2)                  # 100 samples left, then wraps
    assert len(third) == core.CHUNK_BYTES
    assert third[:400] == pcm[-400:] and third[400:] == pcm[:core.CHUNK_BYTES - 400]
    offset = core.chunk_at(pcm, 0, start_sample=7)
    assert offset[:8] == pcm[28:36]


def test_chunk_at_refuses_a_recording_shorter_than_a_chunk():
    with pytest.raises(ValueError):
        core.chunk_at(b"\0" * 100, 0)


def test_pod_start_offsets_are_spread_over_the_recording():
    assert core.pod_start_samples(4, 1000) == [0, 250, 500, 750]
    assert core.pod_start_samples(1, 1000) == [0]


def test_backlog_seconds_matches_the_browser():
    # 12 MB cap ~ 3 min of 16 kHz f32 mono (byod-join-component.jsx:36)
    assert core.backlog_seconds(core.AUDIO_BACKLOG_CAP_BYTES) == pytest.approx(196.61, abs=0.01)
    assert core.backlog_seconds(0) == 0


# --- protocol -----------------------------------------------------------------
def test_start_message_has_every_browser_field_and_passes_server_validation():
    m = core.start_message("12-abc", 12, 7, embeddings=None)
    assert m == {"type": "start", "key": "12-abc", "start_time": 0.0, "sample_rate": 16000,
                 "encoding": "pcm_f32le", "channels": 1, "streamdata": "audio", "tag": True,
                 "embeddings_file": None, "deviceid": 12, "sessionid": 7, "numSpeakers": 0}
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))
    from processing_config_base import validate_start_message
    ok, parsed = validate_start_message(m)
    assert ok, parsed
    assert parsed["encoding"] == "pcm_f32le" and parsed["sample_rate"] == 16000 and parsed["channels"] == 1


def test_speaker_done_message_is_the_roster_confirmation():
    assert core.speaker_done_message() == {"type": "speaker", "id": "done", "speakers": []}


def test_backoff_delay_is_full_jitter_one_to_thirty_seconds():
    assert core.backoff_delay(0, rand=lambda: 0.0) == 0.5
    assert core.backoff_delay(0, rand=lambda: 1.0) == 1.0
    assert core.backoff_delay(3, rand=lambda: 1.0) == 8.0
    assert core.backoff_delay(10, rand=lambda: 1.0) == 30.0
    assert core.backoff_delay(10, rand=lambda: 0.0) == 0.5


def test_names_follow_the_server_character_rules():
    assert core.session_name_ok("LOADTEST 20260930 190000")
    assert not core.session_name_ok("LOADTEST 2026-09-30")       # '-' is not allowed
    assert not core.session_name_ok("Physics 101")               # must be clearly a load test
    assert core.session_name_ok(core.default_session_name(datetime(2026, 9, 30, 19, 0, 0)))
    assert core.NAME_RE.match(core.pod_alias(32))


# --- plan ---------------------------------------------------------------------
def test_ramp_plan_adds_pods_per_step():
    assert core.parse_ramp("2,4,8") == [2, 4, 8]
    assert core.ramp_plan([2, 4, 8], 180) == [(1, 2, 2, 0.0, 180.0), (2, 4, 2, 180.0, 360.0), (3, 8, 4, 360.0, 540.0)]
    assert core.ramp_plan([5], 60) == [(1, 5, 5, 0.0, 60.0)]


@pytest.mark.parametrize("bad", ["", "0", "4,2", "3,3", "40", "2,33"])
def test_ramp_rejects_bad_plans(bad):
    with pytest.raises(ValueError):
        core.parse_ramp(bad)


# --- the session clock and lag ------------------------------------------------
def test_creation_date_parses_both_server_formats():
    assert core.parse_creation_date("2026-09-30 19:00:00 UTC") == datetime(2026, 9, 30, 19, 0, 0)
    assert core.parse_creation_date("2026-09-30 19:00:00.250000 UTC") == datetime(2026, 9, 30, 19, 0, 0, 250000)
    with pytest.raises(ValueError):
        core.parse_creation_date("yesterday")


def test_server_start_offset_mirrors_processing_config():
    created = datetime(2026, 9, 30, 19, 0, 0)
    assert core.server_start_offset(datetime(2026, 9, 30, 19, 1, 30), created) == 90.0
    assert core.server_start_offset(datetime(2026, 9, 30, 19, 1, 30), created, offset=100) == 0.0


def test_lag_from_transcript_json():
    payload = [{"transcript": {"id": 5, "start_time": 90, "length": 4, "transcript": "hi"}, "speaker_metrics": []},
               {"transcript": {"id": 6, "start_time": 100, "length": 7, "transcript": "there"}, "speaker_metrics": []},
               {"transcript": {"id": 7, "start_time": "bad", "length": 1}}]
    rows = list(core.transcript_rows(payload))
    assert core.latest_end(rows) == 107.0
    assert core.max_id(rows) == 7
    # bare transcripts (the /transcripts/client shape) work too
    assert core.latest_end(core.transcript_rows([{"id": 1, "start_time": 3, "length": 2}])) == 5.0
    # pod started 90 s into the session, has sent 40 s of audio: clock = 130
    assert core.caption_lag(130.0, 107.0, 90.0) == 23.0
    # nothing transcribed yet: everything sent so far is behind
    assert core.caption_lag(130.0, None, 90.0) == 40.0
    # rounding can put a caption just past the clock: never negative
    assert core.caption_lag(106.5, 107.0, 90.0) == 0.0
    assert core.latest_end([], current=50.0) == 50.0


def test_drop_warning_count_matches_phase1_texts():
    text = "\n".join([
        "2026-09-30 19:00:00,000 - WARNING: 12-abc: ASR ingest queue full: dropped 3 chunks (0.8s) since last report; 3 of 400 chunks dropped on this connection",
        "2026-09-30 19:00:01,000 - WARNING: CrisperWhisper: transcription backlog full; dropped the window at 120.0s",
        "2026-09-30 19:00:02,000 - WARNING: CrisperWhisper: dropped the window at 132.0s",
        "2026-09-30 19:00:03,000 - INFO: CrisperWhisper: dropped the window at 132.0s",   # not a WARNING
        "2026-09-30 19:00:04,000 - WARNING: CrisperWhisper warm-up failed: x",             # unrelated warning
    ])
    assert core.count_drop_warnings(text) == 3


# --- summary and verdict ------------------------------------------------------
def _row(step, pods, elapsed, pod, lag, ts=None, asr="ok", drops=0, gpu=50, load=2.0):
    return {"step": step, "pods_target": pods, "step_elapsed_s": elapsed, "ts": ts or "t%s" % elapsed,
            "pod": pod, "lag_s": lag, "asr_state": asr, "degraded_events": 0, "drop_warnings": drops,
            "gpu_util": gpu, "gpu_mem_mb": 4000 + gpu, "load1": load}


def test_percentile_is_nearest_rank():
    assert core.percentile([], 95) is None
    assert core.percentile([5], 95) == 5
    assert core.percentile([1, 2, 3, 4, None], 50) == 2
    assert core.percentile(list(range(1, 101)), 95) == 95


def test_summary_and_verdict_pick_the_largest_passing_step():
    rows = []
    for t in (5, 10, 15):                               # step 1: 2 pods, quiet
        rows += [_row(1, 2, t, 1, 10.0), _row(1, 2, t, 2, 12.0)]
    for t in (5, 10, 15):                               # step 2: 4 pods, one bad tick at t=15
        lag = 45.0 if t == 15 else 14.0
        rows += [_row(2, 4, t, p, lag if p == 4 else 14.0, drops=(2 if t == 15 else 0),
                      asr="degraded" if (p == 4 and t == 15) else "ok") for p in (1, 2, 3, 4)]
    for t in (5, 10, 15):                               # step 3: 8 pods, fine but only after warm-up
        rows += [_row(3, 8, t, p, 50.0 if t == 5 else 20.0) for p in range(1, 9)]
    steps, verdict = core.summarize(rows, max_lag_s=30.0, warmup_s=0.0)
    by = {s["step"]: s for s in steps}
    assert by[1]["passed"] and by[1]["median_lag_s"] == 10.0 and by[1]["p95_lag_s"] == 12.0  # nearest rank
    assert not by[2]["passed"] and by[2]["max_lag_s"] == 45.0 and by[2]["degraded_pods"] == [4]
    assert by[2]["drop_warnings"] == 2 and by[2]["worst_tick_p95_s"] == 45.0
    assert not by[3]["passed"]                          # t=5 tick is over the limit
    assert verdict["max_pods_under_lag"] == 2 and verdict["text"].startswith("VERDICT: 2 simultaneous pods")
    # a warm-up excludes the early tick and the 8-pod step passes
    steps, verdict = core.summarize(rows, max_lag_s=30.0, warmup_s=6.0)
    assert {s["step"]: s["passed"] for s in steps} == {1: True, 2: False, 3: True}
    assert verdict["max_pods_under_lag"] == 8
    assert steps[2]["gpu_util_mean"] == 50 and steps[2]["load1_max"] == 2.0


def test_summary_from_csv_strings_and_no_passing_step():
    rows = [_row("1", "2", "5", "1", "40.0"), _row("1", "2", "5", "2", "41.0")]
    steps, verdict = core.summarize(rows, 30.0)
    assert steps[0]["pods"] == 2 and not steps[0]["passed"]
    assert verdict["max_pods_under_lag"] == 0 and verdict["text"].startswith("VERDICT: no step")


# --- the CLI plan, no network -------------------------------------------------
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_dry_run_prints_the_plan_without_touching_the_network(tmp_path, capsys):
    import loadgen
    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True)
    rc = loadgen.main(["--base-url", "http://127.0.0.1:1", "--audio", str(wav), "--ramp", "1,2",
                       "--step-seconds", "10", "--dry-run", "--out", str(tmp_path / "r.csv")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "step 2: 2 pods (+1)" in out and "ws://127.0.0.1:1/audio_socket" in out
    assert '"encoding": "pcm_f32le"' in out and "loadgen2" in out
    assert not (tmp_path / "r.csv").exists()


def test_production_host_needs_the_flag_and_pods_are_capped(tmp_path, capsys):
    import loadgen
    rc = loadgen.main(["--audio", "unused.wav", "--pods", "2", "--dry-run"])   # default base-url is production
    assert rc == 2 and "refusing" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        loadgen.main(["--base-url", "http://127.0.0.1:1", "--audio", "unused.wav", "--pods", "33", "--dry-run"])


def test_snapshot_and_host_fields_fit_the_csv_header():
    import loadgen
    pod = loadgen.Pod(1, api=None, session={"id": 1, "passcode": "x"}, pcm=b"", start_sample=0,
                      ws_url="ws://x/audio_socket", stop=None)
    assert set(pod.snapshot()) <= set(loadgen.CSV_FIELDS)
    assert {"load1", "mem_free_mb", "gpu_util", "gpu_mem_mb", "drop_warnings"} <= set(loadgen.CSV_FIELDS)
