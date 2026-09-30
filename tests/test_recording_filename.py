"""Recording-filename parsing for post-hoc re-analysis.

The audio/video post-hoc services locate a pod's stored recording by filename
and recover two things from it: the pod's processing key (which is the
`source`/`auth_key` every result callback is keyed on) and the recording's
start timestamp. Recordings are written as either

    <key> (Sun Aug  9 00:53:46 2026)_orig.wav   # space before '('
    <key>(Wed Jul 29 16:27:18 2026)_orig.wav    # no space

The old `split("(")[0].split("/")[-1]` left the trailing space on the key for
the first form, so the post-hoc `source` no longer matched
`session_device.processing_key`. Every DB callback (reset, transcript, tagging)
then silently no-oped with HTTP 200 and the re-analysis persisted nothing.
These tests pin the key to the exact processing_key regardless of the spacing.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

from recording_filename import parse_recording_filename  # noqa: E402

KEY = "1323-987a6e43-1533-4b4c-8fbd-88f46af87d24"


def test_space_before_paren_does_not_leak_into_key():
    path = "/data/recordings/{0} (Sun Aug  9 00:53:46 2026)_orig.wav".format(KEY)
    key, off_set_date = parse_recording_filename(path)
    assert key == KEY  # no trailing space
    assert off_set_date == "Sun Aug  9 00:53:46 2026"


def test_no_space_before_paren():
    path = "/data/recordings/{0}(Wed Jul 29 16:27:18 2026)_orig.wav".format(KEY)
    key, off_set_date = parse_recording_filename(path)
    assert key == KEY
    assert off_set_date == "Wed Jul 29 16:27:18 2026"


def test_bare_filename_without_directory():
    key, off_set_date = parse_recording_filename(
        "{0} (Tue Jul 21 19:52:20 2026)_redu.wav".format(KEY))
    assert key == KEY
    assert off_set_date == "Tue Jul 21 19:52:20 2026"


def test_key_is_stripped_of_surrounding_whitespace():
    # Defensive: any incidental whitespace around the key is removed so the
    # source matches processing_key exactly.
    key, _ = parse_recording_filename("/x/  {0}  (Mon Jan 1 00:00:00 2024).wav".format(KEY))
    assert key == KEY
