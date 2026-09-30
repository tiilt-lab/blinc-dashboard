"""Source-level contract tests guarding today's fixes against regression.

These assert on the source text (like test_contracts.py) for changes whose
runtime needs a GPU, a live DB, or Flask — too heavy for CI, but the fix is a
specific line that a careless edit could silently revert. Each test names the
bug it locks in.
"""
import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _read(*parts):
    with open(os.path.join(SRC, *parts)) as f:
        return f.read()


# ---- audio pipeline correctness ------------------------------------------

def test_embed_signal_uses_float_domain():
    # Clustering embeddings must be float-domain (/32768.0); raw int16 was
    # measured as degraded and the merge gate is calibrated in float space.
    s = _read("audio_processing", "speaker_diarization", "pyDiarization.py")
    m = re.search(r"def embedSignal\(.*?encode_batch", s, re.S)
    assert m, "embedSignal signature changed"
    assert "/ 32768.0" in m.group(0), "embedSignal must normalize int16 to float"


def test_embeddings_loaded_with_allow_pickle():
    # The de-forked loader lives in processing_common and MUST allow_pickle
    # (object array); both processors call it. See test_processing_common.py
    # for the behavioral test.
    common = _read("audio_processing", "processing_common.py")
    assert "allow_pickle=True" in common, "shared loader must allow_pickle"
    for mod in ("processor.py", "processor_posthoc.py"):
        assert "load_embeddings(" in _read("audio_processing", mod), \
            f"{mod}: must use the shared load_embeddings"


def test_topic_selection_tracks_best_probability():
    # The de-forked argmax lives in processing_common; both processors call it.
    common = _read("audio_processing", "processing_common.py")
    assert "best_prob" in common, "shared select_topic_id must track the max"
    for mod in ("processor.py", "processor_posthoc.py"):
        assert "select_topic_id(" in _read("audio_processing", mod), \
            f"{mod}: must use the shared select_topic_id"


def test_diarization_flag_not_hardcoded_true():
    s = _read("audio_processing", "processing_config.py")
    assert "self.diarization = diarization" in s
    assert "self.diarization = True" not in s, "diarization must not be hardcoded on"


def test_both_audio_processors_share_the_completion_latch():
    # The completion race (asr-end vs last-worker) must be handled by the one
    # tested CompletionLatch in BOTH processors, not a hand-rolled counter.
    # The posthoc copy had drifted to an unlocked decrement + a direct
    # __complete_callback that bypassed the exactly-once guard.
    for mod in ("processor.py", "processor_posthoc.py"):
        s = _read("audio_processing", mod)
        assert "CompletionLatch(" in s, f"{mod}: must use the shared CompletionLatch"
        assert "self._latch.task_started()" in s and "self._latch.task_done()" in s, \
            f"{mod}: worker lifecycle must go through the latch"
        assert "self._latch.mark_asr_complete()" in s, \
            f"{mod}: end-of-stream must signal the latch"
        assert "running_processes" not in s, \
            f"{mod}: hand-rolled running_processes counter must be gone"
    # The specific drifted pattern must not come back.
    posthoc = _read("audio_processing", "processor_posthoc.py")
    assert "self.running_processes -= 1" not in posthoc
    assert "self.asr_complete and self.running_processes == 0" not in posthoc


# ---- realtime / reactor safety -------------------------------------------

def test_worker_thread_sends_use_reactor_safety_boundary():
    # Twisted transports are not thread-safe; worker-thread sends must go
    # through the reactor_safety boundary (which marshals via callFromThread).
    # The raw-primitive ban is enforced separately in
    # test_reactor_boundary_contract.py.
    checks = [
        ("audio_processing", "processor_posthoc.py"),
        ("audio_processing", "processor_speaker_metric.py"),
        ("audio_processing", "audio_stream_reader.py"),
        ("video_processing", "facial_biometric_processing_service.py"),
        ("server", "device_websockets.py"),
    ]
    for parts in checks:
        s = _read(*parts)
        assert "reactor_safety" in s, f"{'/'.join(parts)}: send must use reactor_safety"


# ---- authorization (round-3 security fixes) ------------------------------

def test_transcript_mutations_are_session_scoped():
    s = _read("server", "routes", "speaker.py")
    # reassign and edit_text must resolve the transcript through the
    # resource-authz layer with WRITE access (fused resolve+authorize).
    assert s.count("authz.resolve_transcript") >= 2, \
        "reassign/edit_text must resolve via authz.resolve_transcript"
    assert "write=True" in s, "transcript mutations must require write access"


def test_remove_device_checks_session_membership():
    s = _read("server", "routes", "session.py")
    m = re.search(r"def remove_device_from_session\(.*?\):(.*?)def ", s, re.S)
    assert m and "session_id" in m.group(1) and "404" in m.group(1), \
        "remove_device_from_session must reject a pod from another session"


def test_updatestudent_is_no_longer_open():
    s = _read("server", "routes", "student.py")
    m = re.search(r"def update_students\(.*?\):(.*?)\n@api_routes", s, re.S)
    assert m, "update_students not found"
    body = m.group(1)
    assert "is_admin" in body and "name_ok" in body, \
        "updatestudent must gate non-admins behind a name-match"


def test_delete_user_relies_on_db_cascades():
    # Metrics, transcripts and pods cascade from the session at the database
    # (migrations e2b3c4d5f6a7 / 2a3b4c5d6e7f). A hand-written cascade here is
    # what broke delete_user before (FK 1451); it must not grow back.
    s = _read("server", "database.py")
    m = re.search(r"def delete_user\(.*?db\.session\.commit\(\)", s, re.S)
    assert m, "delete_user not found"
    body = m.group(0)
    for cls in ("SpeakerVideoMetrics", "SpeakerHrMetrics", "Transcript", "SessionDevice"):
        assert "query(%s)" % cls not in body, "delete_user hand-cascades %s" % cls


def test_device_in_session_guard_is_never_inlined():
    # The cross-tenant isolation predicate ("pod belongs to THIS session, else
    # 404") must go through the single authz.device_in_session helper. Four
    # routes re-implemented `session_device.session_id != session_id` by hand;
    # a hand copy is where the next tenant-isolation leak hides.
    s = _read("server", "routes", "session.py")
    assert ".session_id != session_id" not in s, \
        "tenant-isolation check must use _device_in_session, not an inline predicate"
    assert s.count("_device_in_session(") >= 7, \
        "all membership-guarded routes must resolve via _device_in_session"


def test_connected_flag_cleared_on_session_end_and_removal():
    s = _read("server", "handlers", "session_handler.py")
    assert s.count("connected = False") >= 2, \
        "end_session and remove_session_device must both clear connected"
