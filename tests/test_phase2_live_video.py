"""Live video service, Phase 2 follow-ups (source contracts + the one pure
helper; video_processing/server.py and videoprocessor.py load GPU models
and cv2/torch at import).

  * audit B.7: VideoProcessor.stop() joins its worker OFF the reactor via
    reactor_safety.defer_blocking; signal_end evicts the pod's analytics
    queues only after that join.
  * audit B.3 follow-up: the analytics ffmpeg is capped at 2 decoder threads;
    the optional 720p height cap exists but is OFF (face gates are in native
    pixels); the 10 fps sampling is unchanged.
"""
import ast
import os
import re
import textwrap

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "video_processing")


def _read(*parts):
    with open(os.path.join(SRC, *parts)) as f:
        return f.read()


def _method_source(text, class_name, method):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == method:
                    return textwrap.dedent(ast.get_source_segment(text, fn))
    raise AssertionError("%s.%s not found" % (class_name, method))


# ---- B.7: stop() join off the reactor -------------------------------------

def test_video_processor_stop_joins_off_the_reactor():
    stop = _method_source(_read("video_cartoonizer", "videoprocessor.py"), "VideoProcessor", "stop")
    assert "reactor_safety.defer_blocking(_join)" in stop
    # the bounded join lives inside the deferred function, not at method level
    join_line = next(l for l in stop.splitlines() if "join(timeout=10)" in l)
    assert join_line.startswith("        ")  # nested under def _join
    assert "return reactor_safety.defer_blocking" in stop
    assert "self.running = False" in stop


def test_server_evicts_analytics_queues_after_the_join():
    end = _method_source(_read("server.py"), "ServerProtocol", "signal_end")
    assert "stopping = self.video_processor.stop()" in end
    assert "stopping.addCallback(_evict)" in end and "stopping.addErrback(" in end
    assert "_evict()" in end  # nothing to join -> evict immediately
    evict = end[end.index("def _evict"):]
    assert "frame_queue_manager.pop(key, None)" in evict
    assert "accumulator_queue_manager.pop(key, None)" in evict
    # the direct pops on the reactor path are gone
    assert "frame_queue_manager.pop(self.config.auth_key" not in end


# ---- B.3 follow-up: decoder spawn ------------------------------------------

def _decoder_class():
    s = _read("server.py")
    return s[s.index("class StreamingChunkDecoder"):s.index("cartoon_model = VideoCartoonifyLoader()")]


def test_ffmpeg_spawn_caps_decoder_threads_and_keeps_10_fps():
    d = _decoder_class()
    assert re.search(r"^\s*FPS = 10$", d, re.M)
    assert re.search(r"^\s*DECODE_THREADS = 2$", d, re.M)
    # The command line is built by _ffmpeg_argv (software and cuda variants,
    # pinned in test_video_hwdec.py) and spawned with the class's thread cap.
    argv = _method_source(_read("server.py"), "StreamingChunkDecoder", "_ffmpeg_argv")
    # -threads is an INPUT option (before -i) so it governs the decoder
    assert argv.index("'-threads', str(threads)") < argv.index("'-i', 'pipe:0'")
    assert "'-vf', vf" in argv
    assert "argv = self._ffmpeg_argv(mode, decoder, vf, self.DECODE_THREADS)" in d
    assert "vf = 'fps={0}'.format(self.FPS)" in d


def test_height_cap_exists_but_is_off_and_never_uses_scale_minus_two():
    d = _decoder_class()
    assert re.search(r"^\s*ANALYTICS_MAX_HEIGHT = 0$", d, re.M), \
        "720p rescale must stay off: prepare_quality_face gates faces at native px"
    assert "scale=-2" not in d
    assert "self._analytics_dims(dims[0], dims[1], self.ANALYTICS_MAX_HEIGHT)" in d
    assert "',scale={0}:{1}'.format(self.width, self.height)" in d
    # the reason is recorded next to the switch
    assert "prepare_quality_face" in d


def test_analytics_dims_keeps_native_below_the_cap_and_scales_evenly():
    src = _method_source(_read("server.py"), "StreamingChunkDecoder", "_analytics_dims")
    src = "\n".join(l for l in src.splitlines() if not l.startswith("@"))
    ns = {}
    exec(src, ns)
    dims = ns["_analytics_dims"]
    assert dims(1920, 1080, 0) == (1920, 1080)
    assert dims(1280, 720, 720) == (1280, 720)
    assert dims(1920, 1080, 720) == (1280, 720)
    assert dims(1080, 1920, 720) == (404, 720)      # portrait phone, even width
    assert dims(3840, 2160, 720) == (1280, 720)
    assert dims(1, 2000, 720)[0] % 2 == 0
