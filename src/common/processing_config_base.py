"""Shared validation for the audio & video processing start messages.

Both ProcessingConfig.from_json methods began with the same block: coerce the
numeric fields, check for missing required fields, and allowlist the encoding
and sample rate. That is here once (pure — no redis/callbacks), returning the
parsed primitives; each service's from_json then resolves the session key its
own way (audio reads Redis directly, video goes over HTTP).
"""

ENCODINGS = ['pcm_i16le', 'pcm_f16le', 'pcm_f32le']
SAMPLE_RATES = [16000, 32000, 44100, 48000]


def validate_start_message(data):
    """Validate the common fields of a start message.

    Returns ``(True, parsed)`` where ``parsed`` has auth_key, encoding,
    sample_rate, channels, offset, sessionId, deviceId; or ``(False, error)``
    with a specific message on the first failure.
    """
    auth_key = data.get('key', None)
    encoding = data.get('encoding', None)
    try:
        sample_rate = int(data.get('sample_rate', None))
    except Exception:
        return False, "sample_rate must be an integer."
    try:
        channels = int(data.get('channels', None))
    except Exception:
        return False, "channels must be an integer."
    try:
        # The browser has always sent this as ``start_time`` (the seconds of
        # audio it captured before this connection, e.g. a reconnect backlog);
        # the server only ever read ``offset``, so backlog audio was placed at
        # "now". Accept both names; ``offset`` wins if a client sends both.
        offset = float(data.get('offset', data.get('start_time', 0.0)))
    except Exception:
        return False, "offset must be a float."
    try:
        sessionId = int(data.get('sessionid', None))
    except Exception:
        return False, "sessionid must be an integer."
    try:
        deviceId = int(data.get('deviceid', None))
    except Exception:
        return False, "deviceid must be an integer."

    if not auth_key or not sample_rate or not encoding or not channels or not sessionId or not deviceId:
        return False, "Start message requires key, sample_rate, encoding, sessionid, deviceid and channels."
    if encoding not in ENCODINGS:
        return False, "Unsupported encoding type."
    if sample_rate not in SAMPLE_RATES:
        return False, "Unsupported sample rate."

    return True, {
        'auth_key': auth_key,
        'encoding': encoding,
        'sample_rate': sample_rate,
        'channels': channels,
        'offset': offset,
        'sessionId': sessionId,
        'deviceId': deviceId,
    }
