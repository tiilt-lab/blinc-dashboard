"""Wiring contracts for Phase 2 post-hoc correctness (audit C.1/C.3/C.4/C.5/
C.6, E.2). The services import torch/twisted and cannot run in CI, so these
pin the source: where the GPU lease is taken, that the old results are wiped
only at publish time, that cancel goes through the process-group kill and
releases nothing early, the posthoc_failed route/callback shapes, the
connection-leak fixes, and the fragment-aware lookup.
"""
import ast
import os
import re
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
sys.path.insert(0, os.path.join(SRC, "common"))


def _read(*parts):
    with open(os.path.join(SRC, *parts)) as f:
        return f.read()


def _def(source, name):
    """Source of the first function called ``name`` (methods included)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError("no def %s" % name)


def _before(text, first, second):
    assert first in text and second in text, (first, second)
    assert text.index(first) < text.index(second), "%r must precede %r" % (first, second)


AUDIO = _read("audio_processing", "server_posthoc.py")
VIDEO = _read("video_processing", "server_posthoc.py")
PROC = _read("audio_processing", "processor_posthoc.py")
VPROC = _read("video_processing", "video_cartoonizer", "videoprocessor_posthoc.py")
BASE = _read("audio_processing", "asr_connectors", "base_asr.py")
ROUTES = _read("server", "routes", "callback.py")
COMMON_CB = _read("common", "callbacks_common.py")


# ---- 1. GPU lease + live deferral (audit C.1) --------------------------------

def test_audio_takes_the_lease_in_signal_start_before_any_model():
    body = _def(AUDIO, "signal_start")
    for model in ("AudioBuffer(", "Qwen3ASR(", "WhisperXASR(", "CrisperWhisperPosthocASR(", "GoogleASR(",
                  "AudioProcessorPosthoc("):
        _before(body, "self._gpu_lease = acquire_for_run(", model)
    assert "return False" in body and body.rstrip().endswith("return True")
    assert "owner='audio-posthoc:%d:%s' % (os.getpid(), self.config.auth_key)" in body


def test_audio_replies_gpu_busy_releases_the_pod_claim_and_does_nothing_else():
    handler = AUDIO[AUDIO.index("if not self.signal_start():"):]
    handler = handler[:handler.index("self.processor.setSpeakerFingerprints")]
    assert "running_audio_processes.pop(key, None)" in handler
    assert "self.send_json(dict(GPU_BUSY_REPLY))" in handler
    assert "return" in handler


def test_video_takes_the_lease_before_its_per_run_model_loads():
    body = _def(VIDEO, "process_json")
    _before(body, "self._gpu_lease = acquire_for_run(", "get_emotion_detector(")
    _before(body, "self._gpu_lease = acquire_for_run(", "self.signal_start()")
    busy = body[body.index("if self._gpu_lease is None:"):body.index("get_emotion_detector(")]
    assert "running_video_processes.pop(key, None)" in busy
    assert "self.send_json(dict(GPU_BUSY_REPLY))" in busy and "return" in busy
    assert "owner='video-posthoc:%d:%s' % (os.getpid(), key)" in body


def test_both_servers_release_the_lease_in_a_finally_and_only_mark_complete_on_success():
    for src, name in ((AUDIO, "audio"), (VIDEO, "video")):
        body = _def(src, "on_run_complete")
        assert "success=True" in body.splitlines()[0], name
        assert "finally:" in body and "self._release_gpu()" in body, name
        _before(body, "if success:", "post_posthoc_completed(")
        assert "def _release_gpu(self):" in src, name
        assert "lease.release()" in _def(src, "_release_gpu"), name


# ---- pre-emption by a live class --------------------------------------------

def test_both_servers_start_a_preemption_watcher_on_the_run():
    assert "PreemptionWatcher(redis_client._redis(), on_preempt=self.processor.preempt)" in _def(AUDIO, "signal_start")
    assert "self._preempt_watch.start()" in _def(AUDIO, "signal_start")
    assert "PreemptionWatcher(redis_client._redis(), on_preempt=self.video_processor.preempt)" in _def(VIDEO, "process_json")
    assert "self._preempt_watch.start()" in _def(VIDEO, "process_json")
    for src in (AUDIO, VIDEO):
        assert "watch.stop()" in _def(src, "_release_gpu")


def test_audio_processor_preempts_through_the_cancel_path_and_rechecks_before_the_swap():
    assert "self.cancel()" in _def(PROC, "preempt") and "self._staging.preempt()" in _def(PROC, "preempt")
    done = _def(PROC, "__complete_callback")
    _before(done, "self.live_check()", "self._publish()")
    _before(done, "self._staging.preempt()", "self._publish()")
    assert "live_check=lambda: live_pods_present(redis_client._redis())" in _def(AUDIO, "signal_start")
    assert "PREEMPTED_REPLY" in _def(PROC, "_abandon")


def test_video_processor_has_the_same_hook():
    assert "self.preempted = True" in _def(VPROC, "preempt") and "self.cancel()" in _def(VPROC, "preempt")
    assert "return 'preempted'" in _def(VPROC, "outcome")
    assert "on_finished(success=failure is None, reason=failure)" in _def(VPROC, "processing")
    assert "PREEMPTED_REPLY" in _def(VPROC, "processing")


# ---- 2. old results survive a failed run (audit C.3) --------------------------

def test_audio_marks_running_at_start_without_wiping_and_wipes_only_at_publish():
    assert AUDIO.count("callbacks.post_posthoc_reset(") == 1
    assert "callbacks.post_posthoc_reset(self.config.auth_key, 'audio', wipe=False)" in _def(AUDIO, "signal_start")
    assert PROC.count("callbacks.post_posthoc_reset(") == 1
    publish = _def(PROC, "_publish")
    _before(publish, "staged = self._staging.ordered()", "callbacks.post_posthoc_reset(self.config.auth_key, 'audio')")
    _before(publish, "callbacks.post_posthoc_reset(self.config.auth_key, 'audio')", "callbacks.post_transcripts(**payload)")
    _before(publish, "callbacks.post_posthoc_reset(self.config.auth_key, 'audio')",
            "self.speaker_metrics_process.process_transcript(payload, action=\"posthoc_processing\")")
    _before(publish, "callbacks.post_transcripts(**payload)", "self.send_speaker_taggings()")


def test_audio_processor_stages_instead_of_posting_per_utterance():
    work = _def(PROC, "process_transcript")
    assert "callbacks.post_transcripts(" not in work
    assert "speaker_metrics_process.process_transcript(" not in work
    assert work.count("self._staging.add(") == 2
    assert "if not self.running or not self._staging.accepting:" in work
    assert "self._staging.mark_failed()" in work
    assert "finally:" in work and "self._latch.task_done()" in work
    loop = _def(PROC, "process")
    assert "isinstance(transcript_data, AsrFailed)" in loop
    assert "self._staging.mark_asr_failed(transcript_data.reason)" in loop


def test_audio_processor_abandons_with_posthoc_failed_and_never_marks_complete():
    done = _def(PROC, "__complete_callback")
    assert "self._abandon(failure)" in done and "self._finish(success=failure is None)" in done
    assert "callbacks.post_posthoc_failed(self.config.auth_key, 'audio', reason)" in _def(PROC, "_abandon")
    assert "'code': 'posthoc_failed'" in _def(PROC, "_abandon")
    assert "self.on_finished(success=success)" in _def(PROC, "_finish")


def test_connectors_report_failures_instead_of_swallowing_them():
    for name in ("qwen3_asr.py", "whisperx_asr.py", "crisperwhisper_asr.py"):
        s = _read("audio_processing", "asr_connectors", name)
        assert "self.fail(e)" in s, name
    for name in ("qwen3_asr.py", "crisperwhisper_asr.py"):
        s = _read("audio_processing", "asr_connectors", name)
        assert "handle=getattr(self, 'worker', None)" in s, name
    assert "class AsrFailed" in BASE
    assert "self.transcript_queue.put(AsrFailed(self.error))" in _def(BASE, "fail")


def test_posthoc_failed_route_clears_the_running_flag_and_touches_nothing_else():
    i = ROUTES.index("@api_routes.route('/api/v1/callback/posthoc_failed', methods=['POST'])")
    assert ROUTES[i:].splitlines()[1].strip() == "@wrappers.verify_local"
    body = _def(ROUTES, "posthoc_failed")
    assert "posthoc_state.mark_done(device.id, scope)" in body
    assert "posthoc_state.elapsed_seconds(device.id, scope)" in body
    for forbidden in ("mark_session_device_posthoc", "delete_pod_analysis", "clear_session_device_posthoc"):
        assert forbidden not in body, forbidden
    assert "content.get('reason')" in body
    assert "'posthoc_failed:{0}:{1}'.format(device.id, scope)" in body
    reset = _def(ROUTES, "posthoc_reset")
    assert "if content.get('wipe', True):" in reset
    _before(reset, "if content.get('wipe', True):", "database.delete_pod_analysis(")
    assert "if posthoc_state.elapsed_seconds(device.id, scope) is None:" in reset


def test_posthoc_failed_callback_contract(monkeypatch):
    import callbacks_common
    sent = []
    monkeypatch.setattr(callbacks_common.requests, "post",
                        lambda url, json, timeout: sent.append((url, json, timeout)))
    callbacks_common.post_posthoc_failed("http://127.0.0.1:5001/api/v1/callback", "1323-k", "audio", "x" * 2000)
    callbacks_common.post_posthoc_reset("http://127.0.0.1:5001/api/v1/callback", "1323-k", "audio", wipe=False)
    callbacks_common.post_posthoc_reset("http://127.0.0.1:5001/api/v1/callback", "1323-k", "video")
    assert sent[0][0] == "http://127.0.0.1:5001/api/v1/callback/posthoc_failed"
    assert sent[0][1] == {"source": "1323-k", "scope": "audio", "reason": "x" * 1000}
    assert sent[0][2] == callbacks_common.CALLBACK_TIMEOUT
    assert sent[1][1] == {"source": "1323-k", "scope": "audio", "wipe": False}
    assert sent[2][1] == {"source": "1323-k", "scope": "video", "wipe": True}
    for tree in ("audio_processing", "video_processing"):
        assert "def post_posthoc_failed(source, scope, reason):" in _read(tree, "callbacks.py"), tree
    assert "callbacks.post_posthoc_failed(self.config.auth_key, 'video', reason or 'failed')" in _def(VIDEO, "on_run_complete")


# ---- 3. cancel frees the GPU (audit C.5) --------------------------------------

def test_workers_start_in_their_own_process_group_and_stop_kills_it():
    assert "start_new_session=True" in _def(BASE, "run_json_worker")
    assert "class WorkerHandle" in BASE
    stop = BASE[BASE.index("class PosthocFileASR"):]
    assert "worker.terminate()" in _def(stop, "stop")
    assert "self.worker = WorkerHandle()" in _def(stop, "start")


def test_cancel_goes_through_the_run_and_releases_nothing_early():
    for src, reg in ((AUDIO, "running_audio_processes"), (VIDEO, "running_video_processes")):
        body = _def(src, "process_json")
        cancel = body[body.index("if data['type'] == 'cancel_posthoc':"):body.index("if data['type'] == 'query_posthoc_status':")]
        assert "proc = %s.get(k)" % reg in cancel
        assert "proc.cancel()" in cancel
        _before(cancel, "proc.cancel()", "%s.pop(k, None)" % reg)  # pop only for the flag-only P&I/E&T processors
    cancel = _def(PROC, "cancel")
    for part in ("'asr'", "'reader'", "audio_queue", "stop_signal"):
        assert part in cancel
    assert "on_finished=self.on_run_complete" in _def(AUDIO, "signal_start")
    assert "self.video_processor.on_finished = self.on_run_complete" in _def(VIDEO, "process_json")


# ---- 4. claims (audit C.4) ----------------------------------------------------

def test_stale_claims_are_cleared_at_service_start():
    main = AUDIO[AUDIO.index("if __name__ == '__main__':"):]
    _before(main, "running_audio_processes.clear_stale()", "callbacks.post_service_restarted('audio')")
    _before(main, "cf.initialize()", "running_audio_processes.clear_stale()")
    vmain = VIDEO[VIDEO.index("if __name__ == '__main__':"):]
    _before(vmain, "running_video_processes.clear_stale()", "callbacks.post_service_restarted('video')")


# ---- 5. fragment-aware recordings (audit E.2) ---------------------------------

def test_audio_lookup_orders_and_joins_fragments():
    body = _def(AUDIO, "get_audio_file_path")
    assert "recording_fragments.select_recording(files)" in body
    assert "recording_fragments.join_fragments(to_join, chosen)" in body
    assert "file_path = files[0]" not in AUDIO


# ---- 6. connection leak + dead sweep (audit C.6) -------------------------------

def test_every_connection_leaves_the_manager_and_video_can_be_swept():
    end = _def(AUDIO, "signal_end")
    assert re.search(r"else:\n(\s*#.*\n)*\s*cm\.remove\(self, None, None\)", end), "no-config else branch"
    assert end.count("cm.remove(self, None, None)") == 2, "also when the run continues without this socket"
    assert "def send_close(self, message):" in VIDEO
    vend = _def(VIDEO, "signal_end")
    assert "cm.remove(self," in vend and "self._release_gpu()" in vend


# ---- the ticket gate stays first -----------------------------------------------

def test_ticket_gate_still_precedes_every_handler():
    for src in (AUDIO, VIDEO):
        body = _def(src, "process_json")
        _before(body, "if data['type'] in _TICKETED_TYPES and not self._authorised(data):", "if data['type'] ==")
