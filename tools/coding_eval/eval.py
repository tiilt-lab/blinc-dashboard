#!/usr/bin/env python3
"""Agreement between hand codes and the negotiation-coding model.

    src/venv-unified/bin/python tools/coding_eval/eval.py --gold tools/coding_eval/sample.csv
    ... --dry-run            print the prompts and the request settings; no network
    ... --llm-url URL --model NAME --chunk-size N --context N --timeout S
    ... --out results/<ts>.json   (default: tools/coding_eval/results/<ts>.json; "" to skip)

Gold CSV columns: speaker, start_time_s, text, emotion, rip, frame,
listening (semicolon-separated, may be empty), optional team (the speaker's
side, Pat or Sandy, for the per-team rollup) and optional session (one
conversation per value; rows are coded in file order and chunked per session
the way the engine chunks a transcript, so one prompt never mixes two pods).
Prompt, chunking, LLM call and parsing come from the engine through
adapter.py. See README.md for the metrics.
"""
import argparse
import csv
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adapter  # noqa: E402
import metrics  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REQUIRED_COLUMNS = ("speaker", "start_time_s", "text", "emotion", "rip", "frame", "listening")


class GoldError(ValueError):
    pass


def load_gold(path):
    """Read the hand-coded CSV into engine-shaped utterance dicts
    ({index, speaker_tag, text, ...} plus 'gold'); raises GoldError on bad codes."""
    book = adapter.codebook()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise GoldError("%s: missing column(s) %s" % (path, ", ".join(missing)))
        rows = []
        for lineno, row in enumerate(reader, 2):
            text = (row.get("text") or "").strip()
            if not text:
                continue
            utt = {
                "index": len(rows),
                "line": lineno,
                "speaker_tag": (row.get("speaker") or "").strip(),
                "start_time_s": _time(row.get("start_time_s"), lineno),
                "text": text,
                "team": (row.get("team") or "").strip(),
                "session": (row.get("session") or "").strip(),
                "gold": {},
            }
            for dim in adapter.SINGLE_LABEL:
                v = adapter.normalize_label(dim, row.get(dim))
                if v is None:
                    raise GoldError("line %d: %s=%r is not one of %s" % (
                        lineno, dim, row.get(dim), "/".join(book[dim])))
                utt["gold"][dim] = v
            labels = []
            for item in (row.get("listening") or "").split(";"):
                if not item.strip():
                    continue
                v = adapter.normalize_label("listening", item)
                if v is None:
                    raise GoldError("line %d: listening label %r is not one of %s" % (
                        lineno, item, "/".join(book["listening"])))
                labels.append(v)
            utt["gold"]["listening"] = sorted(set(labels), key=book["listening"].index)
            rows.append(utt)
    if not rows:
        raise GoldError("%s: no utterances" % path)
    return rows


def _time(raw, lineno):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise GoldError("line %d: start_time_s=%r is not a number" % (lineno, raw)) from None


def plan_chunks(utts, size, context):
    """(session, context, to_code) triples, per session (conversation) in
    order of first appearance; a file without a session column is one
    conversation. The team column is the speaker's side and never splits
    the transcript: the model must see both sides to code either."""
    sessions = []
    by_session = {}
    for u in utts:
        key = u.get("session") or ""
        by_session.setdefault(key, []) or sessions.append(key)
        by_session[key].append(u)
    plan = []
    for session in sessions:
        for ctx, chunk in adapter.chunk_utterances(by_session[session], size, context):
            plan.append((session, ctx, chunk))
    return plan


def run_model(plan, url, model, timeout, log=sys.stderr):
    """Code every chunk; returns (codes by utterance index, raw replies, warnings)."""
    codes, raw, warnings = {}, [], []
    for k, (session, ctx, chunk) in enumerate(plan, 1):
        msgs = adapter.build_messages(chunk, ctx)
        print("chunk %d/%d: %d utterances%s ..." % (
            k, len(plan), len(chunk), (" (session %s)" % session) if session else ""), file=log, flush=True)
        text = adapter.call_llm(msgs, url=url, model=model, timeout=timeout)
        parsed, warns = adapter.parse_codes(text, chunk)
        raw.append({"chunk": k, "session": session, "reply": text, "warnings": warns})
        warnings.extend("chunk %d: %s" % (k, w) for w in warns)
        for u, c in zip(chunk, parsed):
            codes[u["index"]] = c
    return codes, raw, warnings


def score(utts, codes):
    book = adapter.codebook()
    per_dim = {}
    for dim in adapter.SINGLE_LABEL:
        gold = [u["gold"][dim] for u in utts]
        pred = [codes[u["index"]][dim] for u in utts]
        per_dim[dim] = metrics.single_label_report(gold, pred, book[dim])
    per_dim["listening"] = metrics.multi_label_report(
        [u["gold"]["listening"] for u in utts],
        [codes[u["index"]]["listening"] for u in utts], book["listening"])
    disagreements = []
    for u in utts:
        m = codes[u["index"]]
        diff = [d for d in adapter.SINGLE_LABEL if u["gold"][d] != m[d]]
        if set(u["gold"]["listening"]) != set(m["listening"]):
            diff.append("listening")
        if diff:
            disagreements.append({
                "index": u["index"], "start_time_s": u["start_time_s"], "speaker": u["speaker_tag"],
                "team": u["team"], "text": u["text"], "dimensions": diff, "gold": u["gold"], "model": m,
            })
    return per_dim, disagreements


def format_report(per_dim, disagreements, n, warnings):
    out = []
    for dim in adapter.SINGLE_LABEL:
        r = per_dim[dim]
        out.append("== %s ==  accuracy %.3f (%d/%d)  Cohen's kappa %.3f" % (
            dim, r["accuracy"], round(r["accuracy"] * r["n"]), r["n"], r["kappa"]))
        out.append(metrics.format_confusion(r["confusion"]))
        out.append("")
    out.append("== listening (multi-label) ==")
    out.append(metrics.format_multi_label(per_dim["listening"]))
    out.append("")
    out.append("== disagreements: %d of %d utterances differ on at least one dimension (%d fully agree) ==" % (
        len(disagreements), n, n - len(disagreements)))
    for d in disagreements:
        t = "t=%7.1fs" % d["start_time_s"] if d["start_time_s"] is not None else "t=      ?"
        out.append("%s %-8s %s" % (t, d["speaker"], _short(d["text"])))
        for dim in d["dimensions"]:
            g, m = d["gold"][dim], d["model"][dim]
            if dim == "listening":
                g, m = "[%s]" % ";".join(g), "[%s]" % ";".join(m)
            out.append("    %-10s gold=%s  model=%s" % (dim, g, m))
    if warnings:
        out.append("")
        out.append("== parse warnings (%d) ==" % len(warnings))
        out.extend("  " + w for w in warnings)
    return "\n".join(out)


def _short(text, n=90):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 3] + "..."


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", required=True, help="hand-coded CSV (see README)")
    ap.add_argument("--llm-url", default=adapter.DEFAULT_LLM_URL)
    ap.add_argument("--model", default=adapter.DEFAULT_MODEL)
    ap.add_argument("--chunk-size", type=int, default=adapter.CHUNK_SIZE,
                    help="utterances per LLM call (engine default %d)" % adapter.CHUNK_SIZE)
    ap.add_argument("--context", type=int, default=adapter.CONTEXT_UTTERANCES,
                    help="preceding utterances shown uncoded before each chunk (engine default %d)"
                         % adapter.CONTEXT_UTTERANCES)
    ap.add_argument("--timeout", type=float, default=adapter.DEFAULT_TIMEOUT_S, help="seconds per LLM call")
    ap.add_argument("--dry-run", action="store_true", help="print the prompts and exit; no network")
    ap.add_argument("--out", default=None,
                    help='results JSON (default tools/coding_eval/results/<timestamp>.json; "" to skip)')
    args = ap.parse_args(argv)

    try:
        utts = load_gold(args.gold)
    except (GoldError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    plan = plan_chunks(utts, args.chunk_size, args.context)

    if args.dry_run:
        print("# dry run: %d utterance(s), %d chunk(s); would POST to %s model %s" % (
            len(utts), len(plan), args.llm_url, args.model))
        for k, (session, ctx, chunk) in enumerate(plan, 1):
            msgs = adapter.build_messages(chunk, ctx)
            body = adapter.request_body(msgs, args.model)
            settings = {k2: v for k2, v in body.items() if k2 != "messages"}
            print("\n### chunk %d/%d%s  request settings: %s" % (
                k, len(plan), (" session=%s" % session) if session else "", json.dumps(settings)))
            for m in msgs:
                print("--- %s ---" % m["role"])
                print(m["content"])
        return 0

    started = _dt.datetime.now()
    codes, raw, warnings = run_model(plan, args.llm_url, args.model, args.timeout)
    per_dim, disagreements = score(utts, codes)
    report = format_report(per_dim, disagreements, len(utts), warnings)
    print(report)

    out = args.out
    if out is None:
        out = os.path.join(HERE, "results", started.strftime("%Y%m%d-%H%M%S") + ".json")
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        payload = {
            "meta": {
                "gold": os.path.abspath(args.gold), "llm_url": args.llm_url, "model": args.model,
                "chunk_size": args.chunk_size, "context": args.context,
                "started": started.isoformat(timespec="seconds"),
                "seconds": round((_dt.datetime.now() - started).total_seconds(), 1),
                "n_utterances": len(utts), "n_chunks": len(plan),
                "engine": os.path.relpath(adapter.ENGINE_PATH, adapter.REPO),
                "codebook_version": adapter.CODEBOOK.get("version"),
            },
            "metrics": per_dim,
            "disagreements": disagreements,
            "utterances": [{"index": u["index"], "speaker": u["speaker_tag"], "start_time_s": u["start_time_s"],
                            "text": u["text"], "team": u["team"], "session": u["session"],
                            "gold": u["gold"], "model": codes[u["index"]]}
                           for u in utts],
            "raw": raw,
            "warnings": warnings,
            "report": report,
        }
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        print("\nresults written to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
