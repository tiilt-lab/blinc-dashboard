"""Raw wearable signals at their native sampling rate, kept apart from the
per-second metrics (speaker_eeg_metrics) for offline analysis.

The join page posts one block per stream per second (muse-eeg.js
RawBlockAssembler): the device's integer counts for every channel,
interleaved sample by sample (s0c0, s0c1, ..., s1c0, ...), as int16
little-endian, base64 in the request. Counts are stored as sent, so nothing
is rounded; SPECS says how a count converts to its unit. A sample lost in
Bluetooth is MISSING and exports as an empty cell, so a block always holds
exactly one second and sample i of a block is at time_ms + i * 1000 / rate.
"""
import base64
import math

MISSING = -32768

# (sensor_kind, stream) -> layout. Muse figures as in muse-eeg.js: EEG
# counts are 12-bit around the 0x800 midpoint (already subtracted by the
# page), the accelerometer's are its own int16.
SPECS = {
    ('muse', 'eeg'): {'channels': ('TP9', 'AF7', 'AF8', 'TP10'), 'rate': 256,
                      'scale': 0.48828125, 'unit': 'uV', 'digits': 3},
    ('muse', 'acc'): {'channels': ('x', 'y', 'z'), 'rate': 52,
                      'scale': 0.0000610352, 'unit': 'g', 'digits': 6},
}


def spec(sensor_kind, stream):
    return SPECS.get((sensor_kind, stream))


def block_bytes(sp):
    """Bytes in one full block: a second of every channel as int16."""
    return sp['rate'] * len(sp['channels']) * 2


def decode_block(sp, data_b64):
    """The posted base64 as stored bytes, or None unless it is exactly one
    block."""
    if not isinstance(data_b64, str):
        return None
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (ValueError, TypeError):
        return None
    return raw if len(raw) == block_bytes(sp) else None


def counts(data):
    """int16 little-endian bytes -> list of ints."""
    return [int.from_bytes(data[i:i + 2], 'little', signed=True) for i in range(0, len(data), 2)]


def missing_count(data):
    return sum(1 for v in counts(data) if v == MISSING)


def header(sp):
    return ['speaker_id', 'speaker_alias', 'sensor_name', 'sample_index', 'time_ms'] + [
        '%s_%s' % (c, sp['unit']) for c in sp['channels']]


def csv_rows(sp, block):
    """One row per sample of a stored block (WearableRawBlock): who, the
    sample's index since the headband connected, its session time in ms and
    each channel in its unit (empty where the sample was lost)."""
    n_ch = len(sp['channels'])
    values = counts(block.data)
    step = 1000.0 / sp['rate']
    for i in range(len(values) // n_ch):
        row = [block.speaker_id if block.speaker_id is not None else '', block.speaker_alias,
               block.sensor_name or '', block.sample_index + i,
               '%.1f' % (block.time_ms + i * step)]
        for v in values[i * n_ch:(i + 1) * n_ch]:
            row.append('' if v == MISSING else round(v * sp['scale'], sp['digits']))
        yield row


def finite_int(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return int(v) if math.isfinite(v) else None
