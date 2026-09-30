"""Thin shim over the negotiation-coding engine (src/server/negotiation_coding.py).

eval.py and the tests talk to the engine only through the functions here, so
the evaluation measures the production prompt, chunking, LLM call and
parse/repair exactly as the feature runs them; if the engine moves or renames
something, this file is the one place to fix. It refuses to run without the
engine (no silent stand-in), because agreement numbers from a different
prompt would be worthless.

Engine contract used (read the engine for details):
    load_codebook()                          -> codebook dict (dimensions -> codes)
    chunks(utts, size, context)              -> (context, chunk) pairs
    build_prompt(codebook, chunk, context)   -> chat messages (user turn ends /no_think)
    call_llm(messages, url, model, timeout)  -> assistant text (temperature 0, thinking off)
    extract_array(text) / validate_codes(items, codebook, indices)
Utterances are dicts with at least {index, speaker_tag, text}; the model
echoes ``index`` back, so chunking never renumbers anything.
"""
import importlib.util
import io
import json
import os
import re
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENGINE_PATH = os.path.join(REPO, "src", "server", "negotiation_coding.py")


def _load_engine(path=ENGINE_PATH):
    if not os.path.exists(path):
        raise ImportError("negotiation-coding engine not found at %s" % path)
    spec = importlib.util.spec_from_file_location("negotiation_coding_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


engine = _load_engine()
CODEBOOK = engine.load_codebook()

DEFAULT_LLM_URL = engine.llm_url()          # honours NEGOTIATION_LLM_URL like production
DEFAULT_MODEL = engine.llm_model()          # honours NEGOTIATION_LLM_MODEL
DEFAULT_TIMEOUT_S = engine.LLM_TIMEOUT
CHUNK_SIZE = engine.CHUNK_SIZE
CONTEXT_UTTERANCES = engine.CONTEXT_SIZE

DIMENSIONS = tuple(CODEBOOK["dimensions"])
SINGLE_LABEL = tuple(d for d, s in CODEBOOK["dimensions"].items() if not s["multi"])
MULTI_LABEL = tuple(d for d, s in CODEBOOK["dimensions"].items() if s["multi"])
DEFAULTS = {d: (list(s["default"]) if s["multi"] else s["default"])
            for d, s in CODEBOOK["dimensions"].items()}


def codebook():
    """Dimension -> ordered list of valid labels."""
    return {dim: list(spec["codes"]) for dim, spec in CODEBOOK["dimensions"].items()}


def normalize_label(dimension, value):
    """A hand-typed label onto the codebook (case, spaces, hyphens), else None.

    Used for the gold CSV only; model output goes through the engine's own
    validation so the eval sees exactly what production would store.
    """
    if value is None:
        return None
    s = re.sub(r"[\s\-]+", "_", str(value).strip().lower())
    return s if s in CODEBOOK["dimensions"][dimension]["codes"] else None


def chunk_utterances(utts, size=CHUNK_SIZE, context=CONTEXT_UTTERANCES):
    """[(context, to_code), ...] exactly as the engine splits a transcript."""
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [(list(ctx), list(chunk)) for ctx, chunk in engine.chunks(utts, size, context)]


def build_messages(to_code, context=()):
    return engine.build_prompt(CODEBOOK, list(to_code), list(context))


def call_llm(messages, url=DEFAULT_LLM_URL, model=DEFAULT_MODEL, timeout=DEFAULT_TIMEOUT_S):
    return engine.call_llm(messages, url=url, model=model, timeout=timeout)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def request_body(messages, model=DEFAULT_MODEL):
    """The JSON body the engine would POST for these messages, captured without
    any network: the engine's call_llm runs against a stand-in urlopen."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["url"] = req.full_url
        return _FakeResponse(b'{"choices": [{"message": {"content": "[]"}}]}')

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        engine.call_llm(messages, url="http://dry-run.invalid/", model=model, retries=0)
    finally:
        urllib.request.urlopen = real
    return captured["body"]


def parse_codes(text, to_code):
    """(codes aligned with to_code, warnings) via the engine's parse/repair.

    A skipped utterance or an unknown label becomes the dimension's default,
    as it would in production; warnings say so instead of raising, so a
    partly broken reply still scores.
    """
    indices = [u["index"] for u in to_code]
    items = engine.extract_array(text)
    warnings = []
    if not items:
        warnings.append("no JSON array of codes in reply; every utterance defaulted")
    by_index, invalid = engine.validate_codes(items, CODEBOOK, indices)
    seen = set()
    for it in items:
        try:
            seen.add(int(it.get("i", it.get("index"))))
        except (AttributeError, TypeError, ValueError):
            continue
    missing = [i for i in indices if i not in seen]
    if missing and items:
        warnings.append("utterance(s) %s missing from reply -> defaults" % ", ".join(map(str, missing)))
    extra = sorted(seen - set(indices))
    if extra:
        warnings.append("reply coded utterance(s) not in this chunk: %s" % ", ".join(map(str, extra)))
    bad_labels = invalid - len(missing)
    if bad_labels > 0:
        warnings.append("%d unknown label(s) replaced by defaults" % bad_labels)
    codes = []
    for i in indices:
        c = by_index[i]
        codes.append({d: (list(c[d]) if isinstance(c[d], list) else c[d]) for d in DIMENSIONS})
    return codes, warnings
