"""Raw wearable signals at their native sampling rate, kept apart from the
per-second metrics (speaker_eeg_metrics) for offline analysis.

The join page posts one block per stream per second (muse-eeg.js and
e4-wristband.js, RawBlockAssembler): the device's integer counts for every
channel, interleaved sample by sample (s0c0, s0c1, ..., s1c0, ...), as
little-endian int16 (int32 where SPECS says width 4), base64 in the request.
Counts are stored as sent, so nothing is rounded; SPECS says how a count
converts to its unit. A sample lost in Bluetooth is MISSING and exports as an
empty cell, so a block always holds exactly one second and sample i of a
block is at time_ms + i * 1000 / rate.
"""
import base64
import math

MISSING = -32768
MISSING_32 = -2147483648

# (sensor_kind, stream) -> layout. A count c exports as c * scale + offset
# (offset 0 unless given), with two exceptions an E4 needs:
#   running_sum: the counts are sample-to-sample differences; the export
#     also gives their running total per wearer (the waveform).
#   reciprocal: the export gives the raw count and reciprocal / count.
# Muse figures as in muse-eeg.js: EEG counts are 12-bit around the 0x800
# midpoint (already subtracted by the page), the accelerometer's are its own
# int16. Empatica E4 figures as in e4-wristband.js (checked on a real band):
# BVP is 7-bit signed differences of the green and red light readings, EDA a
# 24-bit count whose reciprocal is skin conductance, skin temperature 0.02 C
# per count, the accelerometer 1/64 g per count.
SPECS = {
    ('muse', 'eeg'): {'channels': ('TP9', 'AF7', 'AF8', 'TP10'), 'rate': 256,
                      'scale': 0.48828125, 'unit': 'uV', 'digits': 3},
    ('muse', 'acc'): {'channels': ('x', 'y', 'z'), 'rate': 52,
                      'scale': 0.0000610352, 'unit': 'g', 'digits': 6},
    ('e4', 'bvp'): {'channels': ('green', 'red'), 'rate': 64,
                    'scale': 1.0, 'unit': 'delta', 'digits': 0, 'running_sum': True},
    ('e4', 'eda'): {'channels': ('eda',), 'rate': 4, 'width': 4,
                    'scale': 1.0, 'unit': 'raw', 'digits': 6,
                    'reciprocal': 1e6, 'reciprocal_unit': 'uS'},
    ('e4', 'temp'): {'channels': ('temp',), 'rate': 4,
                     'scale': 0.02, 'offset': -275.54, 'unit': 'C', 'digits': 2},
    ('e4', 'acc'): {'channels': ('x', 'y', 'z'), 'rate': 32,
                    'scale': 1.0 / 64, 'unit': 'g', 'digits': 4},
}


def spec(sensor_kind, stream):
    return SPECS.get((sensor_kind, stream))


def width(sp):
    return sp.get('width', 2)


def missing_value(sp):
    return MISSING_32 if width(sp) == 4 else MISSING


def block_bytes(sp):
    """Bytes in one full block: a second of every channel."""
    return sp['rate'] * len(sp['channels']) * width(sp)


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


def counts(data, size=2):
    """Little-endian signed ints of ``size`` bytes -> list of ints."""
    return [int.from_bytes(data[i:i + size], 'little', signed=True) for i in range(0, len(data), size)]


def missing_count(data, sp=None):
    sp = sp or {}
    gone = missing_value(sp)
    return sum(1 for v in counts(data, width(sp)) if v == gone)


def header(sp):
    cols = ['speaker_id', 'speaker_alias', 'sensor_name', 'sample_index', 'time_ms']
    for c in sp['channels']:
        cols.append('%s_%s' % (c, sp['unit']))
        if sp.get('reciprocal'):
            cols.append('%s_%s' % (c, sp['reciprocal_unit']))
    if sp.get('running_sum'):
        cols += list(sp['channels'])
    return cols


def csv_rows(sp, block, state=None):
    """One row per sample of a stored block (WearableRawBlock): who, the
    sample's index since the device connected, its session time in ms and
    each channel in its unit (empty where the sample was lost).

    For a running_sum stream ``state`` (one dict for the whole export,
    blocks in query_raw_blocks order) carries each wearer's totals from block
    to block. They start at 0 when a wearer's device connects (its
    sample_index starts over); a lost sample adds nothing, so after a gap the
    waveform keeps its shape but may sit at a different level."""
    n_ch = len(sp['channels'])
    gone = missing_value(sp)
    values = counts(block.data, width(sp))
    step = 1000.0 / sp['rate']
    scale, offset, digits = sp['scale'], sp.get('offset', 0.0), sp['digits']
    # Unscaled streams (E4 BVP differences, EDA counts) export the count itself.
    as_count = scale == 1.0 and not offset
    sums = None
    if sp.get('running_sum'):
        state = state if state is not None else {}
        key = (block.speaker_id, block.sensor_name)
        prev = state.get(key)
        if prev is None or block.sample_index < prev['next']:
            prev = {'next': 0, 'sums': [0] * n_ch}
        state[key] = prev
        sums = prev['sums']
    for i in range(len(values) // n_ch):
        row = [block.speaker_id if block.speaker_id is not None else '', block.speaker_alias,
               block.sensor_name or '', block.sample_index + i,
               '%.1f' % (block.time_ms + i * step)]
        sample = values[i * n_ch:(i + 1) * n_ch]
        for v in sample:
            if v == gone:
                row.append('')
                if sp.get('reciprocal'):
                    row.append('')
                continue
            row.append(v if as_count else round(v * scale + offset, digits))
            if sp.get('reciprocal'):
                row.append(round(sp['reciprocal'] / v, digits) if v else '')
        if sums is not None:
            for c, v in enumerate(sample):
                if v != gone:
                    sums[c] += v
            row += list(sums)
        yield row
    if sums is not None:
        prev['next'] = block.sample_index + len(values) // n_ch


def finite_int(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return int(v) if math.isfinite(v) else None
