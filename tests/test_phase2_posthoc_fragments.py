"""Fragment-aware post-hoc recording lookup (src/common/recording_fragments.py;
audit E.2). A BYOD reconnect starts a new ``<key> (<ctime>)_orig.wav``; the
lookup used to take ``files[0]`` of an unsorted glob. Ordering, selection,
cache naming and the ffmpeg invocation are checked without ffmpeg.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "common"))

import recording_fragments as rf  # noqa: E402
from recording_filename import parse_recording_filename  # noqa: E402

KEY = "1177-9defadb1-50f2-45ef-a2d0-e8d218cfd098"
A = "/rec/%s (Fri Aug  7 18:30:28 2026)_orig.wav" % KEY
B = "/rec/%s (Fri Aug  7 18:35:37 2026)_orig.wav" % KEY
C = "/rec/%s (Fri Aug  7 18:42:08 2026)_orig.wav" % KEY
A_REDU = "/rec/%s (Fri Aug  7 18:30:28 2026)_redu.wav" % KEY
JOINED = "/rec/%s (Fri Aug  7 18:30:28 2026)_joined.wav" % KEY


def test_fragment_time_parses_ctime_names_including_padded_days():
    assert rf.fragment_time(A) < rf.fragment_time(B) < rf.fragment_time(C)
    assert rf.fragment_time("/x/867-k(Thu May 14 15:29:33 2026)_orig.wav") is not None
    assert rf.fragment_time("/x/867-k_orig.wav") is None


def test_fragments_are_ordered_by_start_time_not_glob_order():
    assert rf.ordered_fragments([C, A_REDU, B, A]) == [A, B, C]


def test_raw_orig_preferred_over_derived_redu_and_redu_used_when_alone():
    assert rf.ordered_fragments([A_REDU, A]) == [A]
    assert rf.ordered_fragments([A_REDU]) == [A_REDU]


def test_join_caches_and_their_temp_files_are_never_fragments():
    leftovers = [JOINED, JOINED[:-4] + ".part.wav", JOINED + ".txt"]
    assert rf.ordered_fragments([B, A] + leftovers) == [A, B]
    assert rf.ordered_fragments(leftovers) == []


def test_unspaced_and_cross_month_names_sort_chronologically():
    may1 = "/x/867-k(Thu May 14 15:29:33 2026)_orig.wav"
    may2 = "/x/867-k(Thu May 14 15:42:17 2026)_orig.wav"
    jul = "/x/867-k (Wed Jul 29 16:27:18 2026)_orig.wav"
    assert rf.ordered_fragments([jul, may2, may1]) == [may1, may2, jul]


def test_nameless_time_sorts_last_and_ties_break_on_name():
    odd = "/rec/%s-extra_orig.wav" % KEY
    assert rf.ordered_fragments([odd, B, A]) == [A, B, odd]


def test_joined_path_round_trips_the_pod_key_and_first_start():
    assert rf.joined_path([A, B, C]) == JOINED
    assert parse_recording_filename(JOINED) == (KEY, "Fri Aug  7 18:30:28 2026")


def test_single_recording_needs_no_join():
    assert rf.select_recording([A]) == (A, None)
    assert rf.select_recording([A_REDU, A]) == (A, None)
    assert rf.select_recording([]) == (None, None)


def test_several_fragments_use_the_join_cache_rebuilt_only_when_stale():
    mtimes = {A: 100.0, B: 200.0, C: 300.0}
    present = set()
    m = lambda p: mtimes[p]           # noqa: E731
    e = lambda p: p in present        # noqa: E731
    assert rf.select_recording([C, B, A], mtime=m, exists=e) == (JOINED, [A, B, C])
    present.add(JOINED)
    mtimes[JOINED] = 250.0            # older than C: rebuild
    assert rf.select_recording([C, B, A], mtime=m, exists=e) == (JOINED, [A, B, C])
    mtimes[JOINED] = 300.0            # as new as the newest fragment: reuse
    assert rf.select_recording([C, B, A, JOINED], mtime=m, exists=e) == (JOINED, None)


def test_non_wav_fragments_are_left_out_of_a_join():
    dat = "/rec/%s (Fri Aug  7 18:35:37 2026)_orig.dat" % KEY
    assert rf.select_recording([A, dat]) == (A, None)
    assert rf.select_recording([dat]) == (dat, None)


def test_concat_list_escapes_single_quotes():
    assert rf.concat_list_text(["/rec/a'b.wav", "/rec/c.wav"]) == \
        "file '/rec/a'\\''b.wav'\nfile '/rec/c.wav'\n"


def test_join_runs_ffmpeg_concat_reencoded_and_renames_atomically(tmp_path):
    out = str(tmp_path / ("%s (Fri Aug  7 18:30:28 2026)_joined.wav" % KEY))
    calls = []

    def fake_run(argv, check, timeout):
        calls.append(argv)
        assert os.path.exists(argv[argv.index("-i") + 1])
        with open(argv[-1], "wb") as f:
            f.write(b"RIFF")
        return subprocess.CompletedProcess(argv, 0)

    assert rf.join_fragments([A, B], out, run=fake_run) == out
    argv = calls[0]
    assert argv[:7] == ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe"]
    assert argv[argv.index("-ar") + 1] == "16000" and argv[argv.index("-acodec") + 1] == "pcm_f32le"
    assert argv[-1] == out[:-4] + ".part.wav", "written to a temp name, renamed on success"
    assert os.path.exists(out) and not os.path.exists(argv[-1]) and not os.path.exists(out + ".txt")


def test_failed_join_leaves_no_cache_behind(tmp_path):
    out = str(tmp_path / ("%s (Fri Aug  7 18:30:28 2026)_joined.wav" % KEY))

    def fake_run(argv, check, timeout):
        with open(argv[-1], "wb") as f:
            f.write(b"partial")
        raise subprocess.CalledProcessError(1, argv)

    try:
        rf.join_fragments([A, B], out, run=fake_run)
        assert False, "expected the ffmpeg failure to propagate"
    except subprocess.CalledProcessError:
        pass
    assert not os.path.exists(out) and not os.listdir(tmp_path)
