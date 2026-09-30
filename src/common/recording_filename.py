"""Parse a stored recording's filename back into (processing_key, start_date).

Shared by the audio and video post-hoc services. Recordings are named

    <key> (Sun Aug  9 00:53:46 2026)_orig.wav   # space before '('
    <key>(Wed Jul 29 16:27:18 2026)_orig.wav    # no space

and the key IS the pod's processing_key — the `source`/`auth_key` that every
result callback (reset, transcript, tagging) is matched on server-side against
session_device.processing_key. The historical `split("(")[0].split("/")[-1]`
left the trailing space on the key for the spaced form, so the source stopped
matching and each callback silently no-oped (HTTP 200, zero rows). Stripping
the key fixes that; the timestamp is stripped too for symmetry.
"""


def parse_recording_filename(audio_file):
    """Return (key, off_set_date) from a recording path or bare filename.

    key           -- the pod processing_key, with directory and any surrounding
                     whitespace removed.
    off_set_date  -- the parenthesised start timestamp, e.g.
                     'Sun Aug  9 00:53:46 2026' (unchanged internal spacing).
    """
    head, _, tail = audio_file.partition("(")
    key = head.split("/")[-1].strip()
    off_set_date = tail.split(")")[0].strip()
    return key, off_set_date
