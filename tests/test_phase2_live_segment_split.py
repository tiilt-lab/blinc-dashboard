"""Batched window embeddings in segment_split (audit B.4).

_window_votes used to call encode_batch once per 1 s window (~30 per
segment). The equal-length windows now go through ONE encode_batch call;
rows are independent in ECAPA so the result matches per-window encodes.
Uses a fake encoder (no model, no GPU).
"""
import os
import sys

import pytest

np = pytest.importorskip("numpy")
torch = pytest.importorskip("torch")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "audio_processing"))

from speaker_diarization import segment_split as ss  # noqa: E402

SR = ss.SR


class FakeEcapa:
    """Deterministic per-row embedding [mean, std, max]; (T,) or (B, T) in,
    (B, 1, 3) out — the shape contract of speechbrain's encode_batch."""

    def __init__(self, fail_batched=False):
        self.calls = []
        self.fail_batched = fail_batched

    def encode_batch(self, sig):
        self.calls.append(tuple(sig.shape))
        if sig.dim() == 1:
            sig = sig.unsqueeze(0)
        elif self.fail_batched:
            raise RuntimeError("CUDA error: out of memory")
        emb = torch.stack([sig.mean(dim=1), sig.std(dim=1), sig.max(dim=1).values], dim=1)
        return emb.unsqueeze(1)


def _audio(seconds, seed=0):
    rng = np.random.RandomState(seed)
    return (rng.randn(int(seconds * SR)) * 3000).astype(np.int16)


def test_batched_embeddings_equal_per_clip_embeddings():
    m = FakeEcapa()
    clips = [_audio(1.0, s) for s in range(5)]
    batched = ss._embed_batch(clips, m)
    assert m.calls == [(5, SR)]
    single = [ss._embed(c, FakeEcapa()) for c in clips]
    for b, s in zip(batched, single):
        assert np.allclose(b, s, atol=1e-6)
        assert abs(np.linalg.norm(b) - 1.0) < 1e-5


def test_unequal_lengths_fall_back_to_per_clip():
    m = FakeEcapa()
    out = ss._embed_batch([_audio(1.0), _audio(0.9)], m)
    assert len(out) == 2 and m.calls == [(SR,), (int(0.9 * SR),)]
    assert ss._embed_batch([], m) == []


def test_window_votes_make_one_encode_call_for_all_windows(monkeypatch):
    m = FakeEcapa()
    monkeypatch.setattr(ss, "_score_prints", lambda utt, fp, v: [(0.9, "ann", "1")])
    votes = ss._window_votes(_audio(6.0), {"1": {"alias": "ann"}}, m)
    # 1 s windows at a 0.5 s hop over 6 s -> 11 windows, ONE batched encode
    assert len(votes) == 11
    assert m.calls == [(11, SR)]
    assert [v[1] for v in votes] == ["ann"] * 11
    assert [round(v[0], 2) for v in votes][:3] == [0.5, 1.0, 1.5]


def test_a_failed_batch_degrades_to_per_window_encodes(monkeypatch):
    m = FakeEcapa(fail_batched=True)
    monkeypatch.setattr(ss, "_score_prints", lambda utt, fp, v: [(0.9, "ann", "1")])
    votes = ss._window_votes(_audio(3.0), {"1": {"alias": "ann"}}, m)
    assert len(votes) == 5
    assert m.calls[0] == (5, SR) and m.calls[1:] == [(SR,)] * 5
    assert all(v[1] == "ann" for v in votes)


def test_a_window_whose_scoring_fails_abstains_alone(monkeypatch):
    m = FakeEcapa()
    n = {"i": 0}

    def scorer(utt, fp, v):
        n["i"] += 1
        if n["i"] == 2:
            raise ValueError("boom")
        return [(0.9, "ann", "1")]

    monkeypatch.setattr(ss, "_score_prints", scorer)
    votes = ss._window_votes(_audio(2.0), {"1": {"alias": "ann"}}, m)
    assert [v[1] for v in votes] == ["ann", None, "ann"]
    assert votes[1][2] == 0.0
