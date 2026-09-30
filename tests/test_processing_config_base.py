"""Shared start-message validation for the audio & video processing configs.

Both services' ProcessingConfig.from_json opened with a byte-identical ~30-line
block: parse sample_rate/channels/offset/sessionid/deviceid, reject missing
fields, and allowlist the encoding + sample rate. That validation lives once in
common/processing_config_base now (pure; the configs themselves import
redis/callbacks). Each from_json then does its own session-key resolution.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

from processing_config_base import validate_start_message  # noqa: E402


def _msg(**over):
    base = {"key": "abc", "encoding": "pcm_f32le", "sample_rate": 48000,
            "channels": 2, "offset": 1.5, "sessionid": 3, "deviceid": 7}
    base.update(over)
    return base


def test_valid_message_returns_parsed_primitives():
    ok, res = validate_start_message(_msg())
    assert ok is True
    assert res == {"auth_key": "abc", "encoding": "pcm_f32le", "sample_rate": 48000,
                   "channels": 2, "offset": 1.5, "sessionId": 3, "deviceId": 7}


def test_non_integer_fields_are_rejected_with_specific_messages():
    assert validate_start_message(_msg(sample_rate="x")) == (False, "sample_rate must be an integer.")
    assert validate_start_message(_msg(channels="x")) == (False, "channels must be an integer.")
    assert validate_start_message(_msg(offset="x")) == (False, "offset must be a float.")
    assert validate_start_message(_msg(sessionid="x")) == (False, "sessionid must be an integer.")
    assert validate_start_message(_msg(deviceid="x")) == (False, "deviceid must be an integer.")


def test_missing_required_field_rejected():
    ok, msg = validate_start_message(_msg(key=None))
    assert ok is False and "requires key" in msg


def test_unsupported_encoding_and_sample_rate():
    assert validate_start_message(_msg(encoding="mp3"))[1] == "Unsupported encoding type."
    assert validate_start_message(_msg(sample_rate=12345))[1] == "Unsupported sample rate."


def test_offset_defaults_to_zero_when_absent():
    m = _msg()
    del m["offset"]
    ok, res = validate_start_message(m)
    assert ok is True and res["offset"] == 0.0


def test_start_time_is_accepted_as_the_offset():
    # The BYOD page sends start_time (its reconnect backlog in seconds); the
    # server used to read only 'offset' and silently ignored it.
    msg = _msg(start_time=42.5)
    del msg['offset']  # the browser never sends 'offset'
    ok, res = validate_start_message(msg)
    assert ok and res['offset'] == 42.5
    ok, res = validate_start_message(_msg(start_time=1.0, offset=7.0))
    assert ok and res['offset'] == 7.0
