"""CrisperWhisper 2.0 worker — runs inside src/venv-crisper.

The crisperwhisper package ships its own CTranslate2 fork
(ctranslate2-crisperwhisper) whose import would shadow the real ctranslate2
that faster-whisper/WhisperX depend on, so it lives in a dedicated venv and
this worker is the only thing that imports it (same isolation pattern as
sortformer_cli / qwen3_worker).

Two modes:
  --oneshot AUDIO OUT.json   transcribe one file, write JSON, exit (post-hoc)
  --serve                    JSON-lines loop on stdin/stdout for the live
                             connector. Prints the ready line after the model
                             loads, then one reply line per request line.
                             Model loads once (~7s), then ~20x realtime on
                             GPU.

--serve protocol (version 2; version 1 requests still work):
  ready line   {"ready": true, "protocol": 2, "batch": true, "model": ...}
               "batch" tells the connector it may send the list form.
  single       {"audio": PATH[, "language":.., "mode":..]}
               -> {"text": str, "words": [[word, start_s, end_s], ...]}
                  or {"error": str}
  list         {"windows": [{"id": ANY, "audio": PATH[, "language", "mode"]}, ...]}
               -> {"results": [{"id": ANY, "text":.., "words":..} |
                               {"id": ANY, "error": str}, ...]}
               one result per window, in request order, each carrying its
               id; one window's failure is its own error entry and never
               loses the others.
  exit         {"exit": true}

Batching note (crisperwhisper 2.0.3, verified in the package source): there
is no multi-audio batch API. ``CrisperWhisperModel.transcribe()`` takes one
audio; ``transcribe_dual()`` batches the *modes* (verbatim + intended) of
ONE audio through the fork's ``generate_dual_greedy`` (shared encoder);
``CT2Engine.generate_batch()`` / ``extract_features_batch()`` are Python
loops over single calls; and the word-timestamp path (attention capture,
hallucination repair, coverage fallback, ``extract_word_timings``) is
single-row only, so the raw CTranslate2 batched ``Whisper.generate`` cannot
be used without losing the timings the live path exists for. The list form
therefore runs its windows sequentially inside this one call: what it saves
is the per-window pipe round trip and Python turnaround, and it lets the
connector hand a free worker several waiting windows at once.
"""
import argparse
import json
import os
import sys

PROTOCOL = 2
DEFAULT_MODEL = "nyralabs/CrisperWhisper2.0_large"
MODEL_ENV = "DC_ASR_MODEL"


def _load_model(model_id, compute_type):
    from crisperwhisper import CrisperWhisperModel
    return CrisperWhisperModel(model_id, compute_type=compute_type)


def _transcribe(model, audio_path, language, mode):
    result = model.transcribe(
        audio_path, language=language, mode=mode, word_timestamps=True
    )
    words = [
        [w.word, round(float(w.start), 3), round(float(w.end), 3)]
        for w in (result.words or [])
    ]
    return {"text": result.text or "", "words": words}


def handle(model, job, language, mode):
    """One request dict -> one reply dict (single or list form)."""
    if "windows" in job:
        results = []
        for index, window in enumerate(job["windows"]):
            try:
                data = _transcribe(
                    model, window["audio"],
                    window.get("language", language), window.get("mode", mode),
                )
            except Exception as e:  # this window only; the rest still run
                data = {"error": str(e)}
            data["id"] = window.get("id", index)
            results.append(data)
        return {"results": results}
    return _transcribe(
        model, job["audio"], job.get("language", language), job.get("mode", mode)
    )


def ready_line(model_id):
    return {"ready": True, "protocol": PROTOCOL, "batch": True, "model": model_id}


def serve(model, lines, out, language, mode, model_id=None):
    """The --serve loop over ``lines`` (an iterable of request lines), replies
    written to ``out``; returns when the input ends or an exit request comes."""
    out.write(json.dumps(ready_line(model_id)) + "\n")
    out.flush()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
            if job.get("exit"):
                break
            data = handle(model, job, language, mode)
        except Exception as e:  # one bad request must not kill the loop
            data = {"error": str(e)}
        out.write(json.dumps(data) + "\n")
        out.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oneshot", nargs=2, metavar=("AUDIO", "OUT"))
    parser.add_argument("--serve", action="store_true")
    # The connector passes --model explicitly (DC_ASR_MODEL, else config.ini);
    # the env default here only covers a hand-started worker.
    parser.add_argument("--model", default=os.environ.get(MODEL_ENV) or DEFAULT_MODEL)
    parser.add_argument("--mode", default="verbatim")
    parser.add_argument("--language", default="en")
    parser.add_argument("--compute-type", default="float16")
    args = parser.parse_args()

    model = _load_model(args.model, args.compute_type)

    if args.oneshot:
        audio, out = args.oneshot
        data = _transcribe(model, audio, args.language, args.mode)
        with open(out, "w") as f:
            json.dump(data, f)
        return

    if args.serve:
        serve(model, sys.stdin, sys.stdout, args.language, args.mode, model_id=args.model)


if __name__ == "__main__":
    main()
