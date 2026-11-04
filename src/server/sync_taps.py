"""Sync taps: one physical event seen by a sensor and by the pod's microphone.

The wearer presses "Sync" on the join page and taps the sensor twice,
firmly. The sensor's accelerometer times each tap on the sensor's own
clock, mapped to the session clock as well as the browser can manage
(sample-clock.js); the knock is also on the pod's audio recording, whose
clock the audio service anchors from stamped capture frames
(clock_anchor.py). The difference is what the sensor's samples still need
to be moved by to land on the audio's clock: Bluetooth delivery latency the
envelope fit could not see, a laptop clock that moved, or, for an imported
device (Empatica E4 CSVs), the device clock's whole offset. A tap at the
start and another at the end also give the drift.

Taps are matched as a pattern, not one by one: speech, chairs and other
knocks make plenty of audio onsets, but only one shift of the sensor's taps
lines up with onsets at the same spacing. Pure numpy, unit-testable.
"""
import numpy as np

HOP_S = 0.002          # envelope resolution: an onset is timed to the hop it rose in
BACKGROUND_S = 0.05    # what a knock is compared against: the 50 ms before it
ONSET_RATIO = 6.0      # a knock's envelope jumps this far above its background (~16 dB)
MIN_GAP_S = 0.08       # two onsets closer than this are one knock (its ring)
MATCH_TOL_S = 0.04     # a sensor tap and an audio onset this close are the same knock
MIN_MATCHED = 2        # a single tap can pair with any onset; two fix the spacing


def detect_onsets(samples, sample_rate, ratio=ONSET_RATIO, min_gap_s=MIN_GAP_S):
    """Seconds (from the first sample) of sharp broadband onsets: a knock
    jumps out of the background within a few ms, while speech swells over
    tens. The first difference stands in for a high-pass (a knock is
    broadband, voiced speech mostly below 1 kHz); a frame is an onset when
    its envelope stands ``ratio`` times above the median of the 50 ms
    before it, and above the window's median by as much, so a quiet room's
    noise flicker never qualifies."""
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    hop = max(1, int(round(sample_rate * HOP_S)))
    n = len(x) // hop
    lookback = max(2, int(round(BACKGROUND_S / HOP_S)))
    if n <= lookback:
        return []
    x = np.diff(x, prepend=x[0])[:n * hop]
    env = np.sqrt((x.reshape(n, hop) ** 2).mean(axis=1))
    floor = max(float(np.median(env)), 1e-12)
    background = np.empty(n)
    background[:lookback] = np.inf
    background[lookback:] = np.median(
        np.lib.stride_tricks.sliding_window_view(env, lookback)[:n - lookback], axis=1)
    hits = (env > ratio * np.maximum(background, 1e-12)) & (env > ratio * floor)
    gap = max(1, int(round(min_gap_s / HOP_S)))
    onsets, last = [], -gap
    for i in np.flatnonzero(hits):
        if i - last >= gap:
            onsets.append(float(i * hop) / sample_rate)
            last = i
    return onsets


def match_taps(sensor_times, audio_times, max_shift_s=2.0, tol_s=MATCH_TOL_S,
               min_matched=MIN_MATCHED):
    """The shift (audio minus sensor, seconds) that lines the most sensor
    taps up with audio onsets, as {offset, residual, matched}; None when no
    shift within ``max_shift_s`` matches at least ``min_matched`` taps.
    Ties go to the tighter fit."""
    sensor = sorted(float(t) for t in sensor_times)
    audio = np.asarray(sorted(float(t) for t in audio_times))
    if len(sensor) < min_matched or len(audio) < min_matched:
        return None
    best = None
    for s in sensor:
        for a in audio:
            shift = a - s
            if abs(shift) > max_shift_s:
                continue
            pairs = []
            for t in sensor:
                d = audio - (t + shift)
                i = int(np.argmin(np.abs(d)))
                if abs(d[i]) <= tol_s:
                    pairs.append(audio[i] - t)
            if len(pairs) < min_matched:
                continue
            offset = float(np.mean(pairs))
            residual = float(np.sqrt(np.mean((np.asarray(pairs) - offset) ** 2)))
            key = (len(pairs), -residual)
            if best is None or key > best[0]:
                best = (key, {'offset': offset, 'residual': residual, 'matched': len(pairs)})
    return best[1] if best else None


LIVE_SHIFT_S = 2.0     # synced sensors: the audio knock is this close to the sensor's tap
IMPORT_SHIFT_S = 10.0  # a device's own clock (E4 export): looser, it was never synced
PAD_S = 1.0            # audio read either side of the search window


def report(markers, read_window):
    """One entry per sync press: its taps, the audio onsets found around
    them and the offset that matches (audio minus sensor, ms; add it to the
    sensor's times to put them on the audio's clock). ``markers`` are
    SyncMarker.json() dicts in time order; ``read_window(start_s, end_s)``
    returns (samples, sample_rate, session seconds of samples[0]) for that
    stretch of the pod's audio, or None when there is none."""
    groups = {}
    for m in markers:
        groups.setdefault(m['sync_id'], []).append(m)
    out = []
    for sync_id, rows in groups.items():
        prompts = [m for m in rows if m['kind'] == 'prompt']
        taps = [m for m in rows if m['kind'] == 'tap']
        named = (taps or rows)[0]
        imported = any(m['source'] == 'import' for m in taps)
        entry = {
            'sync_id': sync_id,
            'speaker_id': named.get('speaker_id'),
            'sensor_kind': named.get('sensor_kind'),
            'sensor_name': named.get('sensor_name'),
            'source': 'import' if imported else 'live',
            'prompt_ms': prompts[0]['time_ms'] if prompts else None,
            'tap_ms': [m['time_ms'] for m in taps],
            'audio_onsets_ms': [],
            'offset_ms': None,
            'residual_ms': None,
            'matched': 0,
            'status': 'too_few_taps',
        }
        out.append(entry)
        if len(taps) < MIN_MATCHED:
            continue
        shift = IMPORT_SHIFT_S if imported else LIVE_SHIFT_S
        tap_s = [m['time_ms'] / 1000.0 for m in taps]
        window = read_window(max(0.0, min(tap_s) - shift - PAD_S), max(tap_s) + shift + PAD_S)
        if window is None:
            entry['status'] = 'no_audio'
            continue
        samples, sample_rate, t0 = window
        onsets = [t0 + o for o in detect_onsets(samples, sample_rate)]
        entry['audio_onsets_ms'] = [int(round(o * 1000.0)) for o in onsets]
        match = match_taps(tap_s, onsets, max_shift_s=shift)
        if match is None:
            entry['status'] = 'no_match'
            continue
        entry.update(offset_ms=int(round(match['offset'] * 1000.0)),
                     residual_ms=round(match['residual'] * 1000.0, 1),
                     matched=match['matched'], status='matched')
    return out
