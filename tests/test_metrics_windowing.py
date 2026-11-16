"""The per-window video-metric reducer shared by utility.py.

utility.py had two byte-near-identical copies of this reduce (the CSV export
path batch_video_metrics, and the synthesis path
aggregate_video_metric_per_window). They had DRIFTED on the empty-window
fallback: the CSV copy emitted avg_attention=None, the synthesis copy emitted
avg_attention=0 for the IDENTICAL no-data window. Unified here on None ("no
attention data"), which is what the CSV already did and is only carried (never
arithmetic'd) downstream in synthesis.

Lives in its own import-light module so CI can test it (utility.py imports
Flask/SQLAlchemy).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "server"))

from metrics_windowing import reduce_video_window, fmt_start_time  # noqa: E402


def test_fmt_start_time_is_hms_everywhere():
    # The three CSV exports formatted the "Start Time" column three ways: two
    # used H:MM:SS, the third leaked raw seconds (str(t.start_time)). One shared
    # formatter, H:MM:SS, for all of them.
    assert fmt_start_time(0) == "0:00:00"
    assert fmt_start_time(65) == "0:01:05"
    assert fmt_start_time(3661) == "1:01:01"
    # Accepts float seconds (DB columns are floats); truncates to whole seconds.
    assert fmt_start_time(65.9) == "0:01:05"


def test_all_three_exports_use_the_shared_start_time_formatter():
    with open(os.path.join(os.path.dirname(__file__), "..", "src", "server",
                           "exports.py")) as f:
        s = f.read()
    assert s.count("fmt_start_time(") >= 3, \
        "all three CSV exports must format Start Time via the shared helper"
    assert "str(t.start_time)," not in s, "raw-seconds Start Time drift must be gone"


def test_empty_window_has_no_attention_not_zero():
    emotion, obj, avg_attention, attention_rate = reduce_video_window([], [], [], 10, 0)
    assert emotion is None
    assert obj is None
    assert avg_attention is None, "an empty window has NO attention data, not 0"
    assert attention_rate == 0


def test_non_empty_window_averages_and_rates():
    emotion, obj, avg_attention, attention_rate = reduce_video_window(
        ["happy", "happy", "sad"], ["screen", "screen", "peer"], [4, 6, 8],
        windowsize=10, window_count=0)
    assert emotion == "happy"
    assert obj == "screen"
    assert avg_attention == (4 + 6 + 8) // 3  # integer average == 6
    assert attention_rate == 6 / (10 * 1)


def test_attention_rate_scales_with_window_index():
    _, _, avg, rate = reduce_video_window([], [], [10], windowsize=5, window_count=3)
    assert avg == 10
    assert rate == 10 / (5 * 4)


def test_both_utility_reducers_use_the_shared_helper():
    with open(os.path.join(os.path.dirname(__file__), "..", "src", "server",
                           "utility.py")) as f:
        s = f.read()
    assert s.count("reduce_video_window(") >= 2, \
        "batch_video_metrics and aggregate_video_metric_per_window must share it"
