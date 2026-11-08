"""Placing wearable samples (heart-rate straps, EEG headbands) on the session clock.

Every metric table counts time from the session's second 0
(session.creation_date, naive UTC). The join page's sensors are timed on the
participant's laptop, whose clock is not the server's. Two ways in:

* ``server_<field>``: the browser already converted its own clock to the
  server's (frontend/src/services/server-clock.js: an NTP-style exchange
  with /api/v1/time, keeping the shortest round trip, so the error is at
  most half that trip, typically 10-50 ms). Used as is.
* ``<field>`` alone (a page loaded before the clock sync shipped): the
  laptop's epoch ms, corrected by the batch's skew, server time when the
  POST arrived minus the laptop's ``client_now`` when it was sent. That
  folds one network trip into every sample, the old behaviour.

Pure, so the rules are unit-testable.
"""
from datetime import timezone


def session_start_ms(creation_date):
    """Epoch ms of the session clock's second 0. creation_date is naive UTC
    (tables/session.py); a naive datetime's .timestamp() reads it as local
    time, which only cancelled out while both sides of the old skew sum made
    the same mistake."""
    return creation_date.replace(tzinfo=timezone.utc).timestamp() * 1000.0


def batch_skew_ms(server_now_ms, client_now):
    """Server minus laptop clock as one batch measured it; 0 without a
    usable ``client_now``."""
    try:
        return float(server_now_ms) - float(client_now)
    except (TypeError, ValueError):
        return 0.0


def server_epoch_ms(sample, field, skew_ms):
    """Server-clock epoch ms of ``sample[field]``: ``server_<field>`` when
    the browser synced its clock, else the laptop time plus the batch skew.
    None when neither is a number."""
    for key, extra in (('server_' + field, 0.0), (field, skew_ms)):
        value = sample.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value) + extra
        except (TypeError, ValueError):
            continue
    return None


def session_times(epoch_ms, start_ms):
    """(time_stamp, time_ms) of an epoch-ms instant on the session clock:
    whole seconds for the integer column every panel and window query uses,
    and milliseconds for alignment. Clamped at 0 like the HR ingest always
    was (a sample from the lobby before the session clock started)."""
    ms = max(0, int(round(float(epoch_ms) - float(start_ms))))
    return ms // 1000, ms


def seconds_to_ms(value):
    """Session seconds (a processor's float stamp) as integer ms; None for
    a missing or non-numeric value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value) * 1000.0))
    except (TypeError, ValueError):
        return None
