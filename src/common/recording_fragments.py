"""Fragment-aware post-hoc recording lookup (audit E.2).

A BYOD reconnect starts a new recording file, so a pod can have several

    <key> (<start ctime>)_orig.wav

fragments (172 pods do). The post-hoc lookup used to take ``files[0]`` of an
unsorted glob — an arbitrary fragment, sometimes the derived ``_redu`` twin.
This module orders the raw ``_orig`` fragments by the start time in their
name and, when there is more than one, joins them in order with ffmpeg's
concat demuxer into a ``_joined.wav`` cache next to them (rebuilt only when a
fragment is newer than the cache). The cache is named so
``parse_recording_filename`` yields the pod key and the FIRST fragment's start,
which is the run's time origin.

Known limit: fragments are butted together, so time after a reconnect gap is
shifted earlier by the outage; before this the later fragments were simply
never analysed. The selection logic is pure and tested without ffmpeg.
"""
import logging
import os
import subprocess
import time

from recording_filename import parse_recording_filename

TIME_FORMAT = '%a %b %d %H:%M:%S %Y'
ORIGINAL_SUFFIX = '_orig'
JOINED_TAG = '_joined'
JOIN_TIMEOUT = 1800


def fragment_time(path):
    """Epoch seconds from the '(<ctime>)' in a recording name, or None."""
    _, stamp = parse_recording_filename(str(path))
    try:
        return time.mktime(time.strptime(stamp, TIME_FORMAT))
    except (ValueError, OverflowError):
        return None


def _stem(path):
    return os.path.splitext(os.path.basename(str(path)))[0]


def is_joined(path):
    # Also matches the '.part' / list files a join in progress leaves behind.
    return JOINED_TAG in os.path.basename(str(path))


def is_original(path):
    return _stem(path).endswith(ORIGINAL_SUFFIX)


def ordered_fragments(paths):
    """The pod's recording fragments, oldest first. Prefers the raw ``_orig``
    captures (``_redu`` is derived from them), ignores earlier join caches,
    orders by the start time in the name (name as tie-break; a nameless-time
    file sorts last)."""
    paths = [str(p) for p in paths if not is_joined(p)]
    originals = [p for p in paths if is_original(p)]
    chosen = originals or paths
    return sorted(chosen, key=lambda p: (fragment_time(p) is None,
                                         fragment_time(p) or 0.0,
                                         os.path.basename(p)))


def joined_path(fragments):
    """Cache file for a join, next to the fragments."""
    first = fragments[0]
    key, stamp = parse_recording_filename(first)
    return os.path.join(os.path.dirname(first), '%s (%s)%s.wav' % (key, stamp, JOINED_TAG))


def cache_is_fresh(cache, fragments, mtime=os.path.getmtime, exists=os.path.exists):
    if not exists(cache):
        return False
    return mtime(cache) >= max(mtime(f) for f in fragments)


def select_recording(paths, mtime=os.path.getmtime, exists=os.path.exists):
    """(path to analyse, fragments to join into it). The second item is None
    when no join is needed: a single recording, or a cache newer than every
    fragment. (None, None) when the pod has no recording."""
    fragments = ordered_fragments(paths)
    if not fragments:
        return None, None
    if len(fragments) == 1:
        return fragments[0], None
    wavs = [f for f in fragments if f.lower().endswith('.wav')]
    if len(wavs) != len(fragments):
        logging.warning('ignoring %d non-wav fragment(s) of %s',
                        len(fragments) - len(wavs), os.path.basename(fragments[0]))
    if len(wavs) <= 1:
        return (wavs[0] if wavs else fragments[0]), None
    cache = joined_path(wavs)
    if cache_is_fresh(cache, wavs, mtime=mtime, exists=exists):
        return cache, None
    return cache, wavs


def concat_list_text(fragments):
    # concat-demuxer list; a quote in a name is closed, escaped, reopened.
    return ''.join("file '%s'\n" % f.replace("'", "'\\''") for f in fragments)


def join_fragments(fragments, out_path, run=subprocess.run, timeout=JOIN_TIMEOUT):
    """Concatenate wav fragments in order into ``out_path`` with ffmpeg's
    concat demuxer, re-encoded to what the post-hoc reader expects (16 kHz
    float PCM; the first fragment's channel layout). Written to a temp name
    and renamed, so a killed ffmpeg never leaves a truncated cache that
    passes the freshness check."""
    list_path = out_path + '.txt'
    part_path = out_path[:-4] + '.part.wav'
    with open(list_path, 'w') as f:
        f.write(concat_list_text(fragments))
    try:
        run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', list_path,
             '-ar', '16000', '-acodec', 'pcm_f32le', part_path],
            check=True, timeout=timeout)
        os.replace(part_path, out_path)
    finally:
        for p in (list_path, part_path):
            try:
                os.remove(p)
            except OSError:
                pass
    return out_path
