"""physio_time: wearable samples on the session clock (pure, no Flask)."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "server"))

import physio_time  # noqa: E402


def test_session_start_reads_creation_date_as_utc_whatever_the_server_zone():
    created = datetime(2026, 11, 3, 15, 0, 0)  # naive UTC, as tables/session.py stores it
    want = datetime(2026, 11, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1000.0
    assert physio_time.session_start_ms(created) == want


def test_a_synced_time_wins_over_the_laptop_time_and_skew():
    sample = {"t": 1000.0, "server_t": 5000.0}
    assert physio_time.server_epoch_ms(sample, "t", skew_ms=123.0) == 5000.0


def test_an_unsynced_sample_falls_back_to_laptop_time_plus_skew():
    assert physio_time.server_epoch_ms({"t": 1000}, "t", skew_ms=250.0) == 1250.0


def test_no_usable_time_is_none():
    assert physio_time.server_epoch_ms({"t": "x"}, "t", 0.0) is None
    assert physio_time.server_epoch_ms({"t": True}, "t", 0.0) is None
    assert physio_time.server_epoch_ms({}, "beat", 0.0) is None


def test_batch_skew_without_client_now_is_zero():
    assert physio_time.batch_skew_ms(10_000.0, None) == 0.0
    assert physio_time.batch_skew_ms(10_000.0, "9000") == 1000.0


def test_session_times_keep_whole_seconds_and_ms_together():
    assert physio_time.session_times(1_000_000 + 63_729.6, 1_000_000) == (63, 63_730)
    assert physio_time.session_times(1_000_000 - 500, 1_000_000) == (0, 0)


def test_seconds_to_ms_rounds_processor_stamps():
    assert physio_time.seconds_to_ms(12.3456) == 12_346
    assert physio_time.seconds_to_ms(None) is None
    assert physio_time.seconds_to_ms("nope") is None
