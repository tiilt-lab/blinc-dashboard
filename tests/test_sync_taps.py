"""sync_taps: finding a sensor's knocks again in the pod's audio."""
import os
import sys

import pytest

np = pytest.importorskip("numpy")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "server"))

import sync_taps  # noqa: E402

SR = 16000


def _knock(signal, at_s, amp=0.8, ring_s=0.03, seed=0):
    # A knock: a broadband burst with a fast exponential decay.
    rng = np.random.default_rng(seed)
    i = int(at_s * SR)
    n = int(ring_s * SR)
    signal[i:i + n] += amp * rng.standard_normal(n) * np.exp(-np.arange(n) / (0.004 * SR))


def _room(seconds, seed=1):
    # Room tone plus a slow "voice" (200 Hz with a swelling envelope).
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    voice = 0.2 * np.sin(2 * np.pi * 200 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t))
    return voice + 0.005 * rng.standard_normal(len(t))


def test_detects_knocks_to_within_a_few_ms_over_speech():
    x = _room(6.0)
    for k, at in enumerate((2.0, 2.45, 4.1)):
        _knock(x, at, seed=k)
    onsets = sync_taps.detect_onsets(x, SR)
    for at in (2.0, 2.45, 4.1):
        assert min(abs(o - at) for o in onsets) < 0.004


def test_one_knock_ringing_is_one_onset():
    x = _room(3.0)
    _knock(x, 1.0, ring_s=0.06)
    near = [o for o in sync_taps.detect_onsets(x, SR) if abs(o - 1.0) < 0.2]
    assert len(near) == 1


def test_match_finds_the_shift_whose_spacing_fits_among_decoys():
    sensor = [10.000, 10.450]
    # The true pair is 0.180 s late; decoys sit closer to the sensor taps
    # but at the wrong spacing.
    audio = [9.950, 10.030, 10.180, 10.630, 10.900]
    m = sync_taps.match_taps(sensor, audio)
    assert m["matched"] == 2
    assert m["offset"] == pytest.approx(0.180, abs=1e-6)


def test_no_match_beyond_the_search_window_or_with_one_tap():
    assert sync_taps.match_taps([10.0, 10.5], [15.0, 15.5], max_shift_s=2.0) is None
    assert sync_taps.match_taps([10.0], [10.1]) is None


def test_report_measures_the_offset_through_the_pod_audio():
    # Audio starts at session second 100; the knocks are at 102.30/102.75
    # on the audio clock, the sensor put them 120 ms early.
    x = _room(6.0)
    _knock(x, 2.30, seed=3)
    _knock(x, 2.75, seed=4)

    def read(start_s, end_s):
        a, b = int((start_s - 100.0) * SR), int((end_s - 100.0) * SR)
        a = max(a, 0)
        return x[a:b], SR, 100.0 + a / SR

    markers = [
        {"sync_id": "s1", "kind": "prompt", "source": "live", "time_ms": 101_500,
         "speaker_id": 7, "sensor_kind": "muse", "sensor_name": "Muse-1A2B"},
        {"sync_id": "s1", "kind": "tap", "source": "live", "time_ms": 102_180,
         "speaker_id": 7, "sensor_kind": "muse", "sensor_name": "Muse-1A2B"},
        {"sync_id": "s1", "kind": "tap", "source": "live", "time_ms": 102_630,
         "speaker_id": 7, "sensor_kind": "muse", "sensor_name": "Muse-1A2B"},
        {"sync_id": "s2", "kind": "prompt", "source": "live", "time_ms": 104_000,
         "speaker_id": 8, "sensor_kind": "muse", "sensor_name": "Muse-3C4D"},
    ]
    out = {r["sync_id"]: r for r in sync_taps.report(markers, read)}
    assert out["s1"]["status"] == "matched"
    assert out["s1"]["offset_ms"] == pytest.approx(120, abs=5)
    assert out["s1"]["prompt_ms"] == 101_500
    assert out["s2"]["status"] == "too_few_taps"


def test_report_without_audio_says_so():
    markers = [{"sync_id": "s", "kind": "tap", "source": "import", "time_ms": t,
                "speaker_id": None, "sensor_kind": "e4", "sensor_name": "E4"} for t in (5000, 5400)]
    out = sync_taps.report(markers, lambda a, b: None)
    assert out[0]["status"] == "no_audio" and out[0]["source"] == "import"
