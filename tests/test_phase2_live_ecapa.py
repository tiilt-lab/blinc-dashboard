"""ECAPA device policy (src/audio_processing/ecapa_device.py) — audit B.4.

The live speaker encoder loads on CUDA when it is available with >= 1.5 GB
free, and falls back to CPU when CUDA is missing, the CUDA load fails, or
VRAM is short. No GPU is touched here: the loader is a fake.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

import ecapa_device as ed  # noqa: E402

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing")


def test_policy_prefers_cuda_and_falls_back_on_low_or_no_gpu():
    assert ed.MIN_FREE_VRAM_MIB == 1500
    assert ed.choose_device(True, 8000) == ("cuda", None)
    assert ed.choose_device(True, None) == ("cuda", None)          # unknown never blocks
    assert ed.choose_device(True, 1499)[0] == "cpu"
    assert "1499 MiB" in ed.choose_device(True, 1499)[1]
    assert ed.choose_device(True, 1500) == ("cuda", None)
    assert ed.choose_device(False, 20000) == ("cpu", "CUDA not available")


class Loader:
    def __init__(self, fail_on_cuda=False):
        self.calls = []
        self.fail_on_cuda = fail_on_cuda

    def __call__(self, source, savedir, run_opts=None):
        self.calls.append(run_opts)
        if run_opts and self.fail_on_cuda:
            raise RuntimeError("CUDA error: out of memory")
        return ("model", run_opts)


def test_loads_on_cuda_with_run_opts_when_allowed():
    ld = Loader()
    model, dev = ed.load_ecapa(ld, "src", "dir", cuda_available=True, free_vram=lambda: 6000)
    assert dev == "cuda" and model == ("model", {"device": "cuda"})
    assert ld.calls == [{"device": "cuda"}]


def test_cpu_when_cuda_is_absent_without_probing_vram():
    ld = Loader()
    probes = []

    def probe():
        probes.append(1)
        return 9999

    model, dev = ed.load_ecapa(ld, "src", "dir", cuda_available=False, free_vram=probe)
    assert dev == "cpu" and model == ("model", None)
    assert ld.calls == [None] and probes == []


def test_cpu_when_free_vram_is_short():
    ld = Loader()
    _, dev = ed.load_ecapa(ld, "src", "dir", cuda_available=True, free_vram=lambda: 1200)
    assert dev == "cpu" and ld.calls == [None]


def test_cuda_load_failure_falls_back_to_cpu(caplog):
    ld = Loader(fail_on_cuda=True)
    with caplog.at_level(logging.INFO):
        model, dev = ed.load_ecapa(ld, "src", "dir", cuda_available=True, free_vram=lambda: None)
    assert dev == "cpu" and model == ("model", None)
    assert ld.calls == [{"device": "cuda"}, None]
    assert any("CUDA load failed" in r.getMessage() for r in caplog.records)


# ---- server.py wiring and embedding compatibility (source contracts) ------

def _read(*parts):
    with open(os.path.join(SRC, *parts)) as f:
        return f.read()


def test_live_server_loads_ecapa_through_the_device_policy():
    s = _read("server.py")
    assert "from ecapa_device import load_ecapa" in s
    assert "load_ecapa(\n    SpeakerRecognition.from_hparams" in s
    assert 'savedir="pretrained_models/pretrained_ecapa")' in s
    # the unconditional CPU load must not come back
    assert 'diarization_model = SpeakerRecognition.from_hparams(' not in s


def test_every_live_encode_moves_embeddings_to_host_memory():
    # With the model on CUDA every consumer must .cpu() before numpy; the
    # .emb.npy cache, enrollment check and live matching all share this model.
    sites = 0
    for parts in (("speaker_diarization", "pyDiarization.py"),
                  ("speaker_diarization", "segment_split.py"),
                  ("enrollment_check.py",)):
        lines = _read(*parts).splitlines()
        for i, line in enumerate(lines):
            if "encode_batch(" in line and "def " not in line:
                sites += 1
                window = "\n".join(lines[i:i + 3])
                assert ".cpu()" in window, (parts, i + 1, window)
    assert sites >= 6, "expected every live encode site to be checked"
