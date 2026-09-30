"""Negotiation coding: every utterance of a pod's transcript coded on four
dimensions (emotion, rights/interests/power, frame, listening moves) by the
local llama-server, rolled up per team and per speaker, plus a de-escalation
timeline. Built for the Kellogg "Viking" case (Pat's team vs Sandy's team);
the codebook is src/server/negotiation_codebook.json.

Import-light on purpose: everything above ``run_coding`` is pure (prompt,
chunking, parsing/repair, rollup, timeline) and runs with a fake LLM in tests;
the database and queue are imported inside the functions that need them.

The LLM contract (verified 2026-09-30 against qwen3.8-27b-uncensored): thinking
must be OFF — ``chat_template_kwargs.enable_thinking=false`` in the request
AND ``/no_think`` at the end of the prompt — or Qwen 3 spends the whole token
budget thinking and returns nothing; temperature 0; strict JSON array back.
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_LLM_MODEL = "qwen3.8-27b-uncensored"
LLM_TIMEOUT = 300
LLM_RETRIES = 2
LLM_MAX_TOKENS = 4096   # ~45 tokens per coded utterance; a chunk needs < 2000
CHUNK_SIZE = 40         # utterances coded per call
CONTEXT_SIZE = 5        # preceding utterances shown read-only
PEAK_WINDOW = 60        # s: the window with the most escalating utterances
CALM_WINDOW = 120       # s: no escalation this long = sustained de-escalation

TEAMS = ("Pat", "Sandy")
UNASSIGNED = "unassigned"
DIMENSIONS = ("emotion", "rip", "frame", "listening")
CODEBOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "negotiation_codebook.json")


def llm_url():
    return os.environ.get("NEGOTIATION_LLM_URL") or DEFAULT_LLM_URL


def llm_model():
    return os.environ.get("NEGOTIATION_LLM_MODEL") or DEFAULT_LLM_MODEL


def load_codebook(path=CODEBOOK_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- prompt

def _codebook_text(codebook):
    # Per dimension: the rule for the whole dimension, one decision rule plus
    # examples per code, the listening conventions, and the calibration
    # examples worded so they never read as extra codes (the model once
    # answered "strong listening" as a code).
    lines = []
    for dim, spec in codebook["dimensions"].items():
        rule = "list every code that applies, [] if none" if spec["multi"] else "exactly one code"
        lines.append("%s (%s). %s" % (dim, rule, spec.get("guidance", "")))
        for code, entry in spec["codes"].items():
            examples = "; ".join('"%s"' % e for e in entry.get("examples") or [])
            lines.append("  - %s: %s%s" % (code, entry["definition"], " e.g. " + examples if examples else ""))
        for convention in spec.get("conventions") or []:
            lines.append("  * " + convention)
        notes = spec.get("notes") or {}
        for kind in ("strong", "weak"):
            if notes.get(kind):
                lines.append("  (examples of %s %s moves, not codes: %s)"
                             % (kind, dim, "; ".join('"%s"' % e for e in notes[kind])))
        lines.append("")
    return "\n".join(lines).rstrip()


def _line(u):
    return "[%d] %s: %s" % (u["index"], u.get("speaker_tag") or "?", (u.get("text") or "").strip())


def build_prompt(codebook, utterances, context):
    """Chat messages coding ``utterances`` with ``context`` (the preceding
    utterances) shown read-only. Each utterance is {index, speaker_tag, text};
    the index is its position in the WHOLE transcript and the model echoes it
    back, so chunking never renumbers anything."""
    facts = "".join("- %s\n" % f for f in codebook.get("case_facts") or [])
    system = (
        "You code utterances from a recorded classroom negotiation exercise (the Viking Investments "
        "case: Pat's team versus Sandy's team). For EVERY utterance in the CODE section give codes on "
        "four dimensions, using ONLY the codes defined here. Judge each utterance by its own "
        "words in the light of what came before; apply the decision rules literally.\n\n"
        + ("CASE FACTS (so references in the transcript resolve):\n" + facts + "\n" if facts else "")
        + _codebook_text(codebook) + "\n\n"
        "Output a JSON array only: no prose, no markdown fence, no thinking. One object per "
        "utterance of the CODE section, in order, shaped "
        '{"i": <index>, "emotion": "<code>", "rip": "<code>", "frame": "<code>", "listening": ["<code>", ...]}. '
        "Never output objects for the CONTEXT section.")
    lines = []
    if context:
        lines.append("CONTEXT (what was said just before; do not code):")
        lines.extend(_line(u) for u in context)
        lines.append("")
    lines.append("CODE (one object per line):")
    lines.extend(_line(u) for u in utterances)
    lines.append("")
    lines.append("/no_think")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)}]


def chunks(utterances, size=CHUNK_SIZE, context=CONTEXT_SIZE):
    """(context, chunk) pairs in transcript order; indices are untouched."""
    for start in range(0, len(utterances), size):
        yield utterances[max(0, start - context):start], utterances[start:start + size]


def utterances_from_rows(rows):
    # Transcript rows -> the dicts the engine works on; index = position.
    return [{"index": i, "transcript_id": r.id, "start_time": r.start_time, "length": r.length,
             "speaker_tag": r.speaker_tag, "text": r.transcript} for i, r in enumerate(rows)]


# ------------------------------------------------------------------- llm

def call_llm(messages, url=None, model=None, timeout=LLM_TIMEOUT, retries=LLM_RETRIES):
    """One chat completion against the OpenAI-compatible llama-server; returns
    the assistant text. Transport/HTTP/shape failures are retried."""
    url = url or llm_url()
    body = json.dumps({
        "model": model or llm_model(),
        "messages": messages,
        "temperature": 0,
        "max_tokens": LLM_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(2 * attempt)
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload["choices"][0]["message"]["content"]
        except (urllib.error.URLError, OSError, ValueError, LookupError, TypeError) as e:
            last = e
            logging.warning("negotiation coding: LLM call failed (attempt %d of %d): %s",
                            attempt + 1, retries + 1, e)
    raise RuntimeError("negotiation LLM at %s failed after %d attempt(s): %s" % (url, retries + 1, last))


# ------------------------------------------------------- parse and repair

_SEP = re.compile(r"[\s,]*")


def extract_array(text):
    """The outermost JSON array of objects in ``text``: tolerates prose or a
    code fence around it, and a truncated tail (every whole object before the
    cut is kept). [] when there is none."""
    text = text or ""
    starts = [m.start() for m in re.finditer(r"\[", text)]
    end = text.rfind("]")
    for s in starts:
        if end <= s:
            break
        try:
            arr = json.loads(text[s:end + 1])
        except ValueError:
            continue
        if isinstance(arr, list) and arr and all(isinstance(x, dict) for x in arr):
            return arr
    decoder = json.JSONDecoder()
    for s in starts:
        items, pos = [], s + 1
        while True:
            pos = _SEP.match(text, pos).end()
            if pos >= len(text) or text[pos] != "{":
                break
            try:
                obj, pos = decoder.raw_decode(text, pos)
            except ValueError:
                break
            items.append(obj)
        if items:
            return items
    return []


def _norm(value):
    if value is None:
        return None
    return str(value).strip().lower().replace(" ", "_").replace("-", "_")


def _coerce(value, spec):
    # (validated value, number of invalid codes seen) for one dimension.
    known = spec["codes"]
    if spec["multi"]:
        if value is None:
            return [], 0
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return [], 1
        kept, bad = [], 0
        for v in value:
            v = _norm(v)
            if v in known:
                if v not in kept:
                    kept.append(v)
            else:
                bad += 1
        return kept, bad
    v = _norm(value)
    if v in known:
        return v, 0
    return spec["default"], 1


def _defaults(codebook):
    return {dim: (list(spec["default"]) if spec["multi"] else spec["default"])
            for dim, spec in codebook["dimensions"].items()}


def validate_codes(items, codebook, indices):
    """{index: codes} for every index in ``indices`` from the model's items.
    An unknown code becomes the dimension's default and counts as invalid; an
    utterance the model skipped gets every default and counts once."""
    by_index = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            i = int(it.get("i", it.get("index")))
        except (TypeError, ValueError):
            continue
        by_index.setdefault(i, it)
    out, invalid = {}, 0
    for i in indices:
        it = by_index.get(i)
        if it is None:
            invalid += 1
            out[i] = _defaults(codebook)
            continue
        codes = {}
        for dim, spec in codebook["dimensions"].items():
            codes[dim], bad = _coerce(it.get(dim), spec)
            invalid += bad
        out[i] = codes
    return out, invalid


def code_utterances(codebook, utterances, llm=None, size=CHUNK_SIZE, context=CONTEXT_SIZE):
    """Every utterance coded, chunk by chunk. Returns (codes aligned with
    ``utterances``, each the utterance dict plus its four codes; invalid_codes)."""
    llm = llm or call_llm
    coded, invalid = [], 0
    for ctx, chunk in chunks(utterances, size, context):
        items = extract_array(llm(build_prompt(codebook, chunk, ctx)))
        if not items:
            raise RuntimeError("LLM returned no parseable codes for utterances %d-%d"
                               % (chunk[0]["index"], chunk[-1]["index"]))
        by_index, bad = validate_codes(items, codebook, [u["index"] for u in chunk])
        invalid += bad
        coded.extend(dict(u, **by_index[u["index"]]) for u in chunk)
    return coded, invalid


# ------------------------------------------------------ rollup, timeline

def team_of(speaker_tag, teams):
    team = (teams or {}).get(speaker_tag)
    return team if team in TEAMS else UNASSIGNED


def _bucket(codebook):
    b = {"utterances": 0}
    for dim, spec in codebook["dimensions"].items():
        b[dim] = {code: 0 for code in spec["codes"]}
    return b


def _tally(bucket, code):
    bucket["utterances"] += 1
    for dim in DIMENSIONS:
        value = code.get(dim)
        for c in (value if isinstance(value, list) else [value]):
            if c in bucket[dim]:
                bucket[dim][c] += 1


def rollup(codes, teams, codebook=None):
    """Counts of every code per team (Pat, Sandy, unassigned) and per speaker
    tag; ``teams`` maps speaker_tag -> team."""
    codebook = codebook or load_codebook()
    by_team = {t: _bucket(codebook) for t in TEAMS + (UNASSIGNED,)}
    by_speaker = {}
    for code in codes:
        tag = code.get("speaker_tag") or ""
        team = team_of(tag, teams)
        _tally(by_team[team], code)
        if tag not in by_speaker:
            by_speaker[tag] = dict(team=team, **_bucket(codebook))
        _tally(by_speaker[tag], code)
    return {"by_team": by_team, "by_speaker": by_speaker}


def timeline(codes):
    """Landmarks in transcript seconds (None when the code never occurs):
    the first escalating utterance; the start of the PEAK_WINDOW with most
    escalating utterances; the first utterance after the first escalation
    that opens a CALM_WINDOW with no escalation (a window the transcript
    ends inside counts: the group stayed calm to the end); the first
    future_problem_solving move."""
    esc = sorted(c["start_time"] for c in codes if c.get("emotion") == "escalating")
    first = esc[0] if esc else None
    peak, peak_n = None, 0
    for t in esc:
        n = sum(1 for e in esc if t <= e < t + PEAK_WINDOW)
        if n > peak_n:
            peak, peak_n = t, n
    calm = None
    if esc:
        for t in sorted(c["start_time"] for c in codes if c["start_time"] > first):
            if not any(t <= e < t + CALM_WINDOW for e in esc):
                calm = t
                break
    future = [c["start_time"] for c in codes if c.get("frame") == "future_problem_solving"]
    return {"first_escalating": first,
            "peak_escalation_window_start": peak,
            "peak_escalation_count": peak_n,
            "first_sustained_deescalation": calm,
            "first_future_move": min(future) if future else None}


OPENING_WINDOW = 180    # s: "how did each team open?" looks at its first 3 minutes
RIP_CODES = ("interest", "right", "power")
QUESTION_CODES = ("open_question", "closed_question", "ask_why", "ask_priority", "ask_constraint")


def team_dynamics(codes, teams):
    """Per team, the debrief's questions: opening_move (dominant rip code in
    the first OPENING_WINDOW seconds of the conversation, "none" if it made
    no rights/interests/power move), rip_sequence (its rip codes in order
    with repeats collapsed and none skipped), last_escalating (seconds of
    its last escalating utterance) and first_interest_question (seconds of
    its first interest-coded question)."""
    ordered = sorted(codes, key=lambda c: (c["start_time"], c.get("transcript_id") or 0))
    start = ordered[0]["start_time"] if ordered else 0
    out = {}
    for team in TEAMS + (UNASSIGNED,):
        mine = [c for c in ordered if team_of(c.get("speaker_tag") or "", teams) == team]
        opening = [c["rip"] for c in mine if c["start_time"] < start + OPENING_WINDOW and c.get("rip") in RIP_CODES]
        # ties go to the code the team reached for first
        counts = {r: opening.count(r) for r in RIP_CODES if r in opening}
        move = max(counts, key=lambda r: (counts[r], -opening.index(r))) if counts else "none"
        sequence = []
        for c in mine:
            if c.get("rip") in RIP_CODES and (not sequence or sequence[-1] != c["rip"]):
                sequence.append(c["rip"])
        esc = [c["start_time"] for c in mine if c.get("emotion") == "escalating"]
        questions = [c["start_time"] for c in mine if c.get("rip") == "interest"
                     and any(q in (c.get("listening") or []) for q in QUESTION_CODES)]
        out[team] = {"opening_move": move, "rip_sequence": sequence,
                     "last_escalating": max(esc) if esc else None,
                     "first_interest_question": min(questions) if questions else None}
    return out


def summarize(codes, teams, codebook=None):
    summary = rollup(codes, teams, codebook)
    for team, dynamics in team_dynamics(codes, teams).items():
        summary["by_team"][team].update(dynamics)
    summary["timeline"] = timeline(codes)
    return summary


def validate_teams(teams):
    """The request's {"<speaker_tag>": "Pat"|"Sandy"} -> (clean dict, error).
    Missing/null means nobody is assigned; empty or null values unassign."""
    if teams is None:
        return {}, None
    if not isinstance(teams, dict):
        return None, "teams must be an object mapping speaker tags to Pat or Sandy."
    clean = {}
    for tag, team in teams.items():
        if team in (None, ""):
            continue
        name = team.strip().capitalize() if isinstance(team, str) else None
        if name not in TEAMS:
            return None, "team for speaker %r must be Pat or Sandy." % tag
        clean[str(tag)] = name
    return clean, None


# --------------------------------------------------------- orchestration

def code_row(code, transcript):
    # One entry of the API's "codes" list: the utterance and its codes.
    return {"transcript_id": transcript.id, "start_time": transcript.start_time,
            "length": transcript.length, "speaker_tag": transcript.speaker_tag,
            "text": transcript.transcript, "emotion": code.emotion, "rip": code.rip,
            "frame": code.frame, "listening": code.listening_list()}


def run_coding(session_device_id, teams=None, llm=call_llm, run_id=None, codebook=None):
    """Code one pod end to end: load its transcripts, code them, persist the
    codes, compute the summary. Reports to run ``run_id`` (created queued by
    the route or end_session) or creates one. Marks the run running -> done,
    or error (message kept) and re-raises so the queue job fails too.
    ``teams`` None = the run row's teams, re-read after coding so a PUT made
    meanwhile wins."""
    import database
    codebook = codebook or load_codebook()
    run = database.get_negotiation_run(run_id) if run_id is not None else None
    if run is None:
        if run_id is not None:
            raise RuntimeError("negotiation coding run %s does not exist" % run_id)
        run = database.create_negotiation_run(session_device_id, llm_model(), codebook["version"], teams or {})
    database.update_negotiation_run(run.id, status="running", started_at=_now(), finished_at=None, error=None)
    try:
        rows = database.get_transcripts(session_device_id=session_device_id)
        coded, invalid = code_utterances(codebook, utterances_from_rows(rows), llm)
        database.add_negotiation_codes(run.id, coded)
        if teams is None:
            teams = database.get_negotiation_run(run.id).teams_dict()
        database.update_negotiation_run(
            run.id, status="done", finished_at=_now(), teams=teams,
            summary=summarize(coded, teams, codebook),
            utterances_coded=len(coded), invalid_codes=invalid)
    except Exception as e:
        logging.exception("negotiation coding: run %s for pod %s failed", run.id, session_device_id)
        database.update_negotiation_run(run.id, status="error", finished_at=_now(), error=str(e)[:2000])
        raise
    return database.get_negotiation_run(run.id)


def recompute_summary(run, teams, codebook=None):
    """New team assignment for a run: re-roll the stored codes, no LLM call."""
    import database
    fields = {"teams": teams}
    if run.status == "done":
        codes = [code_row(c, t) for c, t in database.get_negotiation_codes(run.id)]
        fields["summary"] = summarize(codes, teams, codebook)
    return database.update_negotiation_run(run.id, **fields)


def run_payload(run, with_codes=True):
    """The GET shape: {run, teams, codes?, summary}; summary is None until done."""
    import database
    payload = {"run": run.json(), "teams": run.teams_dict(), "summary": run.summary_dict()}
    if with_codes:
        payload["codes"] = [code_row(c, t) for c, t in database.get_negotiation_codes(run.id)]
    return payload


def queue_coding_run(session_id, session_device_id, teams=None):
    """A run row for the pod and a coding job on the post-hoc queue. A run
    still queued for the pod is reused (its teams updated when given) rather
    than stacking a second identical run behind it."""
    import database
    import posthoc_queue
    run = database.get_latest_negotiation_run(session_device_id)
    if run is not None and run.status == "queued":
        if teams is not None:
            run = database.update_negotiation_run(run.id, teams=teams)
    else:
        run = database.create_negotiation_run(session_device_id, llm_model(),
                                              load_codebook()["version"], teams or {})
    posthoc_queue.enqueue_coding(session_id, session_device_id, run.id)
    return run


def cancel_run(run_id, reason):
    # A queued job dropped from the queue: the row must not say "queued" forever.
    import database
    run = database.get_negotiation_run(run_id)
    if run is not None and run.status in ("queued", "running"):
        database.update_negotiation_run(run_id, status="error", finished_at=_now(), error="cancelled: " + reason)
