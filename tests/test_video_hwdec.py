"""GPU decode for the live analytics feed (src/video_processing/server.py).

server.py loads the GPU models at import, so the pure builders are exec'd
from source (the pattern of test_phase2_live_video.py) and the threading
paths are pinned as source contracts.

The command lines asserted here are the ones checked on the box against a
synthetic VP9 webm (``-f lavfi -i testsrc=size=640x480:rate=20:duration=4``
encoded with libvpx-vp9): the software line and the cuda line each produced
40 rgb24 frames of 640x480x3 bytes (36,864,000 bytes) from the same pipe
input, so the reader and the detectors cannot tell which decoder ran.
"""
import ast
import logging
import os
import textwrap
import time
import types

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "video_processing")


def _server():
    with open(os.path.join(SRC, "server.py")) as f:
        return f.read()


def _strip_decorators(src):
    return "\n".join(l for l in src.splitlines() if not l.startswith("@"))


def _method_source(text, class_name, method):
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == method:
                    return textwrap.dedent(ast.get_source_segment(text, fn))
    raise AssertionError("%s.%s not found" % (class_name, method))


def _top_level(text, names):
    """Source of the module-level functions/assignments called `names`, in
    file order, so they can be exec'd without importing the module."""
    tree = ast.parse(text)
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out.append(ast.get_source_segment(text, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            out.append(ast.get_source_segment(text, node))
    return "\n\n".join(out)


def _helpers():
    ns = {"os": os, "subprocess": None}
    exec(_top_level(_server(), {"CUVID_DECODERS", "CONTAINER_DEFAULT_CODEC",
                                "hwdec_default", "cuvid_decoder", "pod_label"}), ns)
    return ns


def _static(name):
    ns = {}
    exec(_strip_decorators(_method_source(_server(), "StreamingChunkDecoder", name)), ns)
    return ns[name]


# ---- DC_VIDEO_HWDEC policy ---------------------------------------------------

def test_env_wins_and_nvidia_smi_decides_only_when_unset():
    h = _helpers()["hwdec_default"]

    def never():
        raise AssertionError("nvidia-smi must not be probed when DC_VIDEO_HWDEC is set")

    assert h("1", never) is True
    assert h("true", never) is True
    assert h("0", never) is False
    assert h("off", never) is False
    assert h(None, lambda: True) is True
    assert h(None, lambda: False) is False
    assert h("", lambda: True) is True     # blank = unset
    assert h(None, False) is False         # a plain bool works too


def test_default_is_computed_once_at_import_from_env_then_nvidia_smi():
    s = _server()
    assert "HWDEC_ENABLED = hwdec_default(os.environ.get('DC_VIDEO_HWDEC'), _nvidia_smi_ok)" in s
    smi = _top_level(s, {"_nvidia_smi_ok"})
    assert "['nvidia-smi', '-L']" in smi and "returncode == 0" in smi
    assert "except Exception" in smi and "return False" in smi


# ---- decoder choice --------------------------------------------------------------

def test_cuvid_decoder_follows_the_probed_codec_then_the_container():
    pick = _helpers()["cuvid_decoder"]
    assert pick("vp9", "webm") == "vp9_cuvid"
    assert pick("vp8", "webm") == "vp8_cuvid"
    assert pick("h264", "mp4") == "h264_cuvid"
    assert pick("hevc", "mp4") == "hevc_cuvid"
    assert pick("VP9", None) == "vp9_cuvid"
    # no codec from ffprobe: what MediaRecorder puts in each container
    assert pick(None, "webm") == "vp9_cuvid"
    assert pick("", "mp4") == "h264_cuvid"
    # nothing cuvid can decode -> software path
    assert pick("theora", "ogg") is None
    assert pick(None, None) is None


def test_probe_asks_ffprobe_for_the_codec_name_too():
    probe = _method_source(_server(), "StreamingChunkDecoder", "_probe_stream")
    assert "'stream=codec_name,width,height'" in probe
    assert "'pipe:0'" in probe and "timeout=10" in probe


def test_decoder_is_told_the_container_for_the_codec_fallback():
    s = _server()
    start = s[s.index("self.chunk_decoder = StreamingChunkDecoder("):]
    start = start[:start.index(")")]
    assert "container=self.config.mimeExtension" in start


# ---- command lines (verified on the box, see module docstring) --------------

SOFTWARE = ['ffmpeg', '-v', 'error', '-threads', '2', '-i', 'pipe:0',
            '-vf', 'fps=10', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']
CUDA = ['ffmpeg', '-v', 'error', '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda',
        '-c:v', 'vp9_cuvid', '-i', 'pipe:0',
        '-vf', 'fps=10,hwdownload,format=nv12', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']


def test_software_command_line_is_todays():
    argv, hw = _static("_ffmpeg_argv"), _static("_hw_filters")
    assert hw("software") == ""
    assert argv("software", None, "fps=10" + hw("software"), 2) == SOFTWARE


def test_cuda_command_line_keeps_frames_on_the_gpu_until_after_fps():
    argv, hw = _static("_ffmpeg_argv"), _static("_hw_filters")
    vf = "fps=10" + hw("cuda")
    assert argv("cuda", "vp9_cuvid", vf, 2) == CUDA
    # fps drops half the frames BEFORE hwdownload (only kept frames cross
    # PCIe); hwdownload is followed by the format filter ffmpeg-filters(1)
    # says it needs; -pix_fmt rgb24 then converts nv12 exactly as before
    assert vf.index("fps=10") < vf.index("hwdownload") < vf.index("format=nv12")
    assert "-threads" not in argv("cuda", "vp9_cuvid", vf, 2)
    # the h264 (mp4) variant only swaps the decoder
    h264 = argv("cuda", "h264_cuvid", vf, 2)
    assert h264[h264.index("-c:v") + 1] == "h264_cuvid"
    assert [a for a in h264 if a != "h264_cuvid"] == [a for a in CUDA if a != "vp9_cuvid"]


def test_output_contract_is_unchanged_for_both_modes():
    argv = _static("_ffmpeg_argv")
    for mode, dec in (("software", None), ("cuda", "vp9_cuvid")):
        a = argv(mode, dec, "fps=10", 2)
        assert a[-7:] == ['-vf', 'fps=10', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1']
        assert a[a.index("-f") + 1] == "rawvideo"
        assert a[a.index("-i") + 1] == "pipe:0"


def test_respawn_builds_the_scale_after_the_hw_filters_and_uses_the_class_threads():
    r = _method_source(_server(), "StreamingChunkDecoder", "_respawn")
    assert "vf = 'fps={0}'.format(self.FPS) + self._hw_filters(mode)" in r
    assert r.index("self._hw_filters(mode)") < r.index("',scale={0}:{1}'.format(self.width, self.height)")
    assert "argv = self._ffmpeg_argv(mode, decoder, vf, self.DECODE_THREADS)" in r
    assert "if self.hwdec and not self.hw_failed:" in r
    assert "decoder = cuvid_decoder(self.codec, self.container)" in r
    # stderr is kept (DEVNULL hid why a decoder died)
    assert "stderr=subprocess.PIPE" in r and "stderr=subprocess.DEVNULL" not in r
    assert "self.decode_mode = mode" in r
    assert "metrics.set_decode_mode(pod_label(self.label), mode)" in r


# ---- fallback: hardware ffmpeg dies within 5 s -> software for this pod -------

class _Proc:
    def __init__(self, rc):
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc


def _decoder_stub(**kw):
    stub = types.SimpleNamespace(HWDEC_FALLBACK_SECONDS=5.0, dead=False, hw_failed=False,
                                 respawn_wanted=False, label="12-abc", _stderr_tail=lambda: "")
    for k, v in kw.items():
        setattr(stub, k, v)
    return stub


def test_fallback_window_is_five_seconds():
    s = _server()
    assert "HWDEC_FALLBACK_SECONDS = 5.0" in s
    assert "HWDEC_FALLBACK_SECONDS = HWDEC_FALLBACK_SECONDS" in s   # class attribute


def test_early_nonzero_exit_of_the_cuda_ffmpeg_flips_the_pod_to_software():
    ns = {"time": time, "logging": logging}
    exec(_method_source(_server(), "StreamingChunkDecoder", "_note_exit"), ns)
    note = ns["_note_exit"]
    now = time.monotonic()

    d = _decoder_stub()
    note(d, _Proc(1), "cuda", now - 1.0)
    assert d.hw_failed is True and d.respawn_wanted is True

    # a late failure is a pipe break, not a decoder failure: keep cuda
    d = _decoder_stub()
    note(d, _Proc(1), "cuda", now - 6.0)
    assert d.hw_failed is False and d.respawn_wanted is False

    # clean exit, our own kill (signal), software mode, or teardown: no flip
    for proc, mode, dead in ((_Proc(0), "cuda", False), (_Proc(-9), "cuda", False),
                             (_Proc(1), "software", False), (_Proc(1), "cuda", True),
                             (_Proc(None), "cuda", False)):
        d = _decoder_stub(dead=dead)
        note(d, proc, mode, now - 1.0)
        assert d.hw_failed is False and d.respawn_wanted is False


def test_reader_epilogue_runs_on_every_exit_and_the_pump_acts_on_it():
    s = _server()
    read = _method_source(s, "StreamingChunkDecoder", "_read_frames")
    fin = read[read.rindex("finally:"):]
    assert "force_sentinel(batch_q)" in fin
    assert "self._note_exit(proc, mode, spawned_at or time.monotonic())" in fin
    pump = _method_source(s, "StreamingChunkDecoder", "_pump")
    assert "if self.proc is None or is_header or self.respawn_wanted:" in pump
    # the pipe-break retry replays the cached header so the chunk is not lost
    assert "self._restart(self.init_blob, replay_header=True)" in pump
    restart = _method_source(s, "StreamingChunkDecoder", "_restart")
    assert "self.respawn_wanted = False" in restart
    # the pump waits for the reader's verdict before spawning again
    kill = _method_source(s, "StreamingChunkDecoder", "_kill_proc")
    assert "reader.join(timeout=5)" in kill


# ---- decode_mode + exporter ---------------------------------------------------

def test_pod_label_is_the_session_device_id_never_the_key():
    label = _helpers()["pod_label"]
    assert label("12-4f3c9a2e-1111-2222-3333-444455556666") == "12"
    assert label("no key") == "no key"
    assert label(None) == "unknown"
    assert "4f3c9a2e" not in label("12-4f3c9a2e-1111-2222-3333-444455556666")


def test_decode_mode_is_exposed_per_pod_and_cleared_at_stop():
    s = _server()
    prop = _method_source(s, "ServerProtocol", "decode_mode")
    assert "return self.chunk_decoder.decode_mode if self.chunk_decoder is not None else None" in prop
    stop = _method_source(s, "StreamingChunkDecoder", "stop")
    assert "metrics.clear_pod(pod_label(self.label))" in stop
    init = _method_source(s, "StreamingChunkDecoder", "__init__")
    assert "self.decode_mode = None" in init and "self.hw_failed = False" in init
    assert "self.hwdec = HWDEC_ENABLED if hwdec is None else bool(hwdec)" in init


def test_exporter_is_self_contained_import_guarded_and_loopback():
    s = _server()
    assert "try:\n    import prometheus_client as _prom\nexcept Exception:" in s
    assert "METRICS_ADDR = os.environ.get('DC_VIDEO_METRICS_ADDR', '127.0.0.1')" in s
    assert "METRICS_PORT = int(os.environ.get('DC_VIDEO_METRICS_PORT', '9112'))" in s
    assert "_prom.start_http_server(int(port), addr=addr, registry=self.registry)" in s
    for name in ("'blinc_video_pods'", "'blinc_video_batches_dropped_total'", "'blinc_video_decode_mode'"):
        assert name in s, name
    assert "['pod'], registry=self.registry" in s
    main = s[s.index("if __name__ == '__main__':"):]
    assert "metrics.serve()" in main
    assert "task.LoopingCall(_metrics_sample)" in main
    # the shared wrapper (another workstream) is not imported
    assert "import metrics" not in s and "from metrics" not in s


def test_dropped_batches_total_stays_monotonic_across_pod_eviction():
    s = _server()
    end = _method_source(s, "ServerProtocol", "signal_end")
    evict = end[end.index("def _evict"):]
    assert "gone = image_object_detection.frame_queue_manager.pop(key, None)" in evict
    assert "_dropped_retired['batches'] += int(getattr(gone, 'dropped', 0) or 0)" in evict
    total = _top_level(s, {"_batches_dropped_total"})
    assert "_dropped_retired['batches'] + live" in total


def test_metrics_gauges_functional():
    prom = pytest.importorskip("prometheus_client")
    s = _server()
    tree = ast.parse(s)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VideoMetrics")
    ns = {"_prom": prom, "logging": logging, "METRICS_PORT": 9112, "METRICS_ADDR": "127.0.0.1"}
    exec(ast.get_source_segment(s, cls), ns)
    reg = prom.CollectorRegistry()
    m = ns["VideoMetrics"](registry=reg)
    assert m.enabled and not m.served
    m.set_decode_mode("12", "cuda")
    m.set_decode_mode("13", "software")
    m.set_decode_mode("14", "bogus")   # ignored
    m.sample(2, 7)
    get = reg.get_sample_value
    assert get("blinc_video_decode_mode", {"pod": "12"}) == 1
    assert get("blinc_video_decode_mode", {"pod": "13"}) == 0
    assert get("blinc_video_decode_mode", {"pod": "14"}) is None
    assert get("blinc_video_pods") == 2
    assert get("blinc_video_batches_dropped_total") == 7
    m.clear_pod("12")
    m.clear_pod("12")   # idempotent
    assert get("blinc_video_decode_mode", {"pod": "12"}) is None
    assert get("blinc_video_decode_mode", {"pod": "13"}) == 0


# ---- functional: the decoder class off the GPU ----------------------------------
# The class is exec'd from source with the module globals it touches stubbed;
# needs ffmpeg (the synthetic webm is made with lavfi testsrc) and numpy.

import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import queue as _queue  # noqa: E402

np = pytest.importorskip("numpy")


def _decoder_class(hwdec_enabled, cuvid):
    s = _server()
    tree = ast.parse(s)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "StreamingChunkDecoder")
    modes = []
    ns = dict(queue=_queue, Full=_queue.Full, subprocess=subprocess, threading=threading,
              logging=logging, time=time, np=np, HWDEC_ENABLED=hwdec_enabled,
              HWDEC_FALLBACK_SECONDS=5.0,
              metrics=types.SimpleNamespace(set_decode_mode=lambda pod, mode: modes.append((pod, mode)),
                                            clear_pod=lambda pod: modes.append((pod, None))),
              pod_label=lambda k: k.split("-", 1)[0], cuvid_decoder=cuvid)
    exec(ast.get_source_segment(s, cls), ns)
    return ns["StreamingChunkDecoder"], modes


@pytest.fixture(scope="module")
def synthetic_webm(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg not installed")
    path = str(tmp_path_factory.mktemp("hwdec") / "testsrc.webm")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc=size=640x480:rate=20:duration=2",
                    "-c:v", "libvpx-vp9", "-b:v", "300k", "-pix_fmt", "yuv420p", path],
                   check=True, timeout=120)
    with open(path, "rb") as f:
        return f.read()


def _decode(Dec, blob, label, hwdec, chunks=1, want=18):
    frames, shapes, lock = [], set(), threading.Lock()

    def sink(it):
        def consume():
            for _ts, frame in it:
                with lock:
                    frames.append(_ts)
                    shapes.add(frame.shape)
        threading.Thread(target=consume, daemon=True).start()

    d = Dec(1, sink, label, container="webm", hwdec=hwdec)
    step = len(blob) // chunks + 1
    for i in range(chunks):
        d.feed(blob[i * step:(i + 1) * step])
        time.sleep(0.3)
    t0 = time.time()
    while time.time() - t0 < 20 and len(frames) < want:
        time.sleep(0.1)
    n = len(frames)
    d.stop()
    d.pump.join(timeout=10)
    assert not d.pump.is_alive(), "pump must exit after stop()"
    return d, n, shapes


def test_software_decode_streams_rgb24_frames_of_the_probed_size(synthetic_webm):
    Dec, modes = _decoder_class(False, lambda c, cont: None)
    # 2 s at 20 fps sampled to 10 fps = 20 frames; the fps filter holds the
    # last one until EOF, which only stop() delivers.
    d, n, shapes = _decode(Dec, synthetic_webm, "11-sw", hwdec=False, want=19)
    assert n >= 19 and shapes == {(480, 640, 3)}
    assert (d.width, d.height, d.codec, d.decode_mode) == (640, 480, "vp9", "software")
    assert modes == [("11", "software"), ("11", None)]


def test_hardware_ffmpeg_that_exits_at_once_falls_back_to_software_and_still_decodes(synthetic_webm):
    Dec, modes = _decoder_class(True, lambda c, cont: "vp9_cuvid")
    real = Dec._ffmpeg_argv
    # the "hardware" ffmpeg: exits non-zero immediately (no GPU is touched)
    Dec._ffmpeg_argv = staticmethod(
        lambda mode, decoder, vf, threads: ["sh", "-c", "exit 3"] if mode == "cuda"
        else real(mode, decoder, vf, threads))
    d, n, shapes = _decode(Dec, synthetic_webm, "12-hw", hwdec=True, chunks=3, want=19)
    assert d.hw_failed is True and d.decode_mode == "software"
    assert n >= 19 and shapes == {(480, 640, 3)}
    # cuda was reported for the pod, then software, then cleared at stop
    assert modes == [("12", "cuda"), ("12", "software"), ("12", None)]
