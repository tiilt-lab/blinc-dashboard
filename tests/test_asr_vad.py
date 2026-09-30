"""Voice-activity gate in front of the live CrisperWhisper pool (asr_ingest).

Most classroom pods are silent much of the time, yet every 12 s window used
to cost ~1 s of GPU. The gate skips windows with no speech: a window with
speech in any 0.5 s frame is transcribed whole (never trimmed), the first
two windows of a connection always are, a skipped window still advances the
pod's timeline (starts come from absolute sample offsets), and skips are
counted and reported once a minute.

Two detectors: EnergyGate (RMS per frame against an adaptive noise floor,
with hysteresis; fails open above -38 dBFS) and SileroDetector (Silero VAD,
ONNX on the CPU). Synthetic audio covers silence, a tone, white noise at
-40 dBFS and a speech-like AM burst; the Silero positive case uses the real
speech sample shipped with pyannote.audio (a whisperx dependency).
"""
import logging
import os
import queue
import sys
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

import asr_ingest as ai  # noqa: E402
from asr_ingest import (EnergyGate, SileroDetector, SpeechGate, frame_levels_db,  # noqa: E402
                        make_speech_gate, pcm_to_float, vad_mode)

SR = 16000
WINDOW = 12.0


# --- synthetic audio ----------------------------------------------------------

def to_pcm(x):
    return (np.clip(np.asarray(x, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes()


def at_db(x, db):
    rms = np.sqrt(np.mean(np.square(x))) or 1.0
    return (x / rms * 10 ** (db / 20.0)).astype(np.float32)


def silence(seconds=WINDOW):
    return np.zeros(int(seconds * SR), np.float32)


def tone(seconds=WINDOW, freq=440.0, db=-20.0):
    t = np.arange(int(seconds * SR)) / SR
    return at_db(np.sin(2 * np.pi * freq * t), db)


def white_noise(seconds=WINDOW, db=-40.0, seed=0):
    return at_db(np.random.default_rng(seed).standard_normal(int(seconds * SR)), db)


def am_burst(seconds=WINDOW, db=-25.0, f0=150.0, syllable_hz=4.0):
    """Speech-like: a harmonic complex (f0 up to 3 kHz) amplitude-modulated
    at syllable rate, RMS at ``db`` over the whole burst."""
    t = np.arange(int(seconds * SR)) / SR
    carrier = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, int(3000 // f0) + 1))
    envelope = 0.5 * (1 + np.sin(2 * np.pi * syllable_hz * t - np.pi / 2)) ** 2
    return at_db(carrier * envelope, db)


def mix(base, insert, at_seconds):
    out = np.array(base, dtype=np.float32)
    i = int(at_seconds * SR)
    out[i:i + len(insert)] += insert[:len(out) - i]
    return out


def speech_sample_window():
    """12 s of real speech from pyannote.audio's bundled sample, or None.

    Located without importing the (heavy) package: find_spec only resolves
    where it lives."""
    import importlib.util
    try:
        spec = importlib.util.find_spec("pyannote.audio")
    except Exception:
        spec = None
    if spec is None or not spec.submodule_search_locations:
        return None
    path = os.path.join(list(spec.submodule_search_locations)[0], "sample", "sample.wav")
    if not os.path.exists(path):
        return None
    with wave.open(path, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SR):
            return None
        w.setpos(12 * SR)
        return pcm_to_float(w.readframes(int(WINDOW * SR)))


def gate(detector, **kw):
    return SpeechGate(detector, **kw)


def admitted(g, x, now=None):
    return g.admit(to_pcm(x), now=now)[0]


# --- frame levels --------------------------------------------------------------

def test_frame_levels_are_rms_dbfs_per_half_second_ignoring_a_partial_tail():
    levels = frame_levels_db(tone(12.3, db=-20.0))
    assert levels.shape == (24,)                       # 12.3 s -> 24 full frames
    assert np.allclose(levels, -20.0, atol=0.1)
    assert (frame_levels_db(silence()) == ai.SILENCE_DB).all()
    assert frame_levels_db(silence(0.4)).size == 0


def test_pcm_to_float_scales_int16():
    assert pcm_to_float(b"\x00\x80\xff\x7f\x00\x00").tolist() == [-1.0, 32767 / 32768.0, 0.0]


# --- energy gate ---------------------------------------------------------------

def test_energy_gate_transcribes_the_first_two_windows_then_skips_silence():
    g = gate(EnergyGate())
    assert [admitted(g, silence()) for _ in range(4)] == [True, True, False, False]
    assert g.windows_seen == 4 and g.windows_skipped == 2 and g.skip_ratio == 0.5


def test_energy_gate_learns_the_room_and_skips_steady_noise_at_minus_40():
    g = gate(EnergyGate())
    noise = [white_noise(seed=k) for k in range(4)]
    assert [admitted(g, n) for n in noise] == [True, True, False, False]
    on, off = g.detector.thresholds()
    assert -41 < g.detector.noise_floor_db < -39           # 20th percentile of -40 dBFS frames
    assert on == -38.0 and off == -38.0                     # floor + 6 clamped to the ceiling; no
                                                            # hysteresis below floor + 3 (= -37)


def test_energy_gate_keeps_a_speech_burst_over_the_noise_and_forgets_it_after():
    g = gate(EnergyGate())
    for k in range(3):
        admitted(g, white_noise(seed=k))
    window = mix(white_noise(seed=7), am_burst(2.0), at_seconds=5.0)   # 2 s of "speech" at 5 s
    assert admitted(g, window)                                         # whole window, not trimmed
    assert not admitted(g, white_noise(seed=8))                        # burst didn't lift the floor
    assert g.windows_skipped == 2


def test_energy_gate_never_suppresses_clear_speech_it_fails_open_in_a_loud_room():
    g = gate(EnergyGate())
    loud_room = [white_noise(db=-32.0, seed=k) for k in range(5)]      # ambient above the ceiling
    assert all(admitted(g, w) for w in loud_room)                      # nothing skipped, ever
    assert admitted(g, mix(white_noise(db=-32.0, seed=9), am_burst(3.0, db=-25.0), 4.0))
    assert g.windows_skipped == 0
    assert g.detector.thresholds()[0] == -38.0                         # floor + 6 would be -26


def test_energy_gate_hysteresis_keeps_a_quiet_trailing_frame():
    d = EnergyGate()
    g = gate(d)
    for _ in range(2):
        admitted(g, white_noise(db=-70.0))                             # a very quiet room
    on, off = d.thresholds()
    assert on == -60.0 and off == -63.0                                # floor + 6 clamped up to -60
    assert admitted(g, white_noise(db=-50.0, seed=1))                  # speech level: in speech
    trailing = np.concatenate([white_noise(0.5, db=-62.0, seed=2), white_noise(11.5, db=-70.0, seed=3)])
    assert admitted(g, trailing)                                       # -62 > off while in speech
    assert not admitted(g, white_noise(db=-62.0, seed=4))              # -62 < on once out of speech


def test_energy_gate_cannot_tell_a_loud_tone_from_speech():
    g = gate(EnergyGate())
    assert [admitted(g, tone()) for _ in range(3)] == [True, True, True]


def test_energy_gate_admits_a_window_shorter_than_a_frame_without_judging_it():
    g = gate(EnergyGate())
    for _ in range(3):
        admitted(g, silence())
    assert admitted(g, silence(0.3)) and g.windows_skipped == 1        # the flush tail


# --- silero detector (mechanics with an injected probability function) -------

def test_silero_scans_512_sample_chunks_in_order_and_stops_at_the_first_speech_chunk():
    seen = []

    def prob(chunk):
        seen.append(len(chunk))
        return 0.9 if len(seen) == 6 else 0.1

    d = SileroDetector(chunk_prob=prob)
    assert d.has_speech(to_pcm(silence()))
    assert seen == [512] * 6                                           # early exit
    seen.clear()
    d = SileroDetector(chunk_prob=lambda c: 0.2)
    assert not d.has_speech(to_pcm(silence()))
    d = SileroDetector(chunk_prob=lambda c: 0.5)                       # threshold is inclusive
    assert d.has_speech(to_pcm(silence()))


def test_silero_needs_16k_audio():
    with pytest.raises(ValueError):
        SileroDetector(sample_rate=8000, chunk_prob=lambda c: 0.0)


# --- silero detector (the real ONNX model) -----------------------------------

def test_silero_import_does_not_leave_torch_on_one_thread():
    pytest.importorskip("silero_vad")
    import torch
    before = torch.get_num_threads()
    ai._silero.clear()                                                 # force the import path
    ai.load_silero_model()
    assert torch.get_num_threads() == before


def test_silero_skips_silence_tone_and_noise_but_keeps_real_speech():
    pytest.importorskip("silero_vad")
    pytest.importorskip("onnxruntime")
    speech = speech_sample_window()
    if speech is None:
        pytest.skip("pyannote.audio sample.wav not available for a real-speech fixture")
    g = gate(SileroDetector())
    assert admitted(g, silence()) and admitted(g, silence())           # warm-up
    assert not admitted(g, silence())
    assert not admitted(g, tone())                                     # energy would pass this
    assert not admitted(g, white_noise())
    assert not admitted(g, am_burst())                                 # synthetic, not speech
    assert admitted(g, speech)
    assert admitted(g, mix(silence(), speech[:SR // 2], at_seconds=8.0))   # 0.5 s of speech: whole window
    assert g.windows_skipped == 4 and g.windows_seen == 8


# --- the per-connection gate -------------------------------------------------

class Fixed:
    name = "fixed"

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = 0

    def has_speech(self, pcm):
        self.asked += 1
        return self.answers.pop(0)


def test_warmup_windows_are_transcribed_but_the_detector_still_sees_them():
    d = Fixed([False, False, False, True])
    g = gate(d)
    assert [admitted(g, silence()) for _ in range(4)] == [True, True, False, True]
    assert d.asked == 4 and g.windows_skipped == 1


def test_gate_reports_once_a_minute_with_the_ratio():
    g = gate(Fixed([False] * 3 + [True] * 3 + [False] * 4), log_interval=60.0)
    pcm = to_pcm(silence())
    texts = [g.admit(pcm, now=100.0 + 5 * k)[1] for k in range(6)]     # 25 s: no report yet
    assert texts == [None] * 6
    ok, text = g.admit(pcm, now=161.0)                                 # first window past a minute
    assert ok is False and "ASR VAD (fixed): skipped 2 of 7 windows in the last 61s (29%)" in text
    assert "2 of 7 on this connection (29%)" in text
    assert [g.admit(pcm, now=170.0)[1], g.admit(pcm, now=180.0)[1]] == [None, None]
    ok, text = g.admit(pcm, now=225.0)
    assert "skipped 3 of 3 windows in the last 64s (100%)" in text and "5 of 10 on this connection (50%)" in text
    assert g.skip_ratio == 0.5


# --- mode selection ----------------------------------------------------------

def test_vad_mode_defaults_to_silero_when_importable_else_energy():
    assert vad_mode({}, available=True) == "silero"
    assert vad_mode({}, available=False) == "energy"
    assert vad_mode({"DC_ASR_VAD": "off"}, available=True) == "off"
    assert vad_mode({"DC_ASR_VAD": "Energy"}, available=True) == "energy"
    assert vad_mode({"DC_ASR_VAD": "silero"}, available=False) == "energy"   # falls back, never off
    assert vad_mode({"DC_ASR_VAD": "loud"}, available=False) == "energy"
    assert vad_mode(os.environ, available=None) in ("silero", "energy")


def test_make_speech_gate_off_energy_and_silero():
    assert make_speech_gate("off") is None
    assert make_speech_gate(environ={"DC_ASR_VAD": "off"}) is None
    assert make_speech_gate("energy").name == "energy"
    if ai.silero_available():
        assert make_speech_gate("silero").name == "silero"
    with pytest.raises(ValueError):
        make_speech_gate("loud")


# --- flush thread integration (connector) ------------------------------------

from asr_connectors import crisperwhisper_asr as cw  # noqa: E402


class FakePool:
    def __init__(self):
        self.calls = []

    def transcribe(self, wav_path):
        with wave.open(wav_path, "rb") as w:
            self.calls.append(w.getnframes())
        return {"words": [["hi", 0.0, 0.5]]}


def run_flush(monkeypatch, windows, gate_factory):
    pool = FakePool()
    monkeypatch.setattr(cw, "_pool", pool)
    transcripts = queue.Queue()
    asr = cw.CrisperWhisperASR(queue.Queue(), transcripts, None, "audio", 0.25)
    asr.gate_factory = gate_factory
    for start, x in windows:
        asr._window_queue.put((start, to_pcm(x)))
    asr._window_queue.put(None)
    asr._transcribing()
    starts = []
    while True:
        r = transcripts.get_nowait()
        if r is None:
            break
        starts.append(r.alternatives[0].words[0].start_time.seconds)
    return asr, pool, starts


def test_flush_thread_skips_silent_windows_and_the_next_window_keeps_its_absolute_start(monkeypatch):
    windows = [(0.0, am_burst()), (12.0, am_burst()), (24.0, silence()),
               (36.0, silence()), (48.0, am_burst()), (60.0, silence(0.2))]
    asr, pool, starts = run_flush(monkeypatch, windows, lambda: SpeechGate(EnergyGate()))
    assert len(pool.calls) == 4                                        # 2 warm-up + burst + short tail
    assert starts == [0, 12, 48, 60]                                   # no drift across the skips
    assert asr.windows_skipped == 2 and asr.vad_skip_ratio == pytest.approx(2 / 6)


def test_with_the_gate_off_every_window_goes_to_the_pool(monkeypatch):
    windows = [(12.0 * k, silence()) for k in range(4)]
    asr, pool, starts = run_flush(monkeypatch, windows, lambda: None)
    assert len(pool.calls) == 4 and asr.vad_skip_ratio == 0.0


def test_a_broken_detector_fails_open(monkeypatch, caplog):
    class Broken:
        name = "broken"

        def has_speech(self, pcm):
            raise RuntimeError("onnx exploded")

    windows = [(12.0 * k, silence()) for k in range(4)]
    with caplog.at_level(logging.WARNING):
        asr, pool, _ = run_flush(monkeypatch, windows, lambda: SpeechGate(Broken()))
    assert len(pool.calls) == 4 and asr._gate is None
    assert any("onnx exploded" in r.getMessage() for r in caplog.records)


def test_a_gate_that_cannot_be_built_fails_open(monkeypatch):
    def boom():
        raise ImportError("no silero here")

    windows = [(12.0 * k, silence()) for k in range(3)]
    asr, pool, _ = run_flush(monkeypatch, windows, boom)
    assert len(pool.calls) == 3 and asr._gate is None
