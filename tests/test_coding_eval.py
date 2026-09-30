"""Unit tests for tools/coding_eval (no network).

Pins the agreement metrics to hand-computed values, the gold CSV parser, the
adapter's view of the engine (prompt rules, request settings, parse/repair)
and that ``eval.py --dry-run`` runs without touching the network.
"""
import importlib.util
import os
import subprocess
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TOOL_DIR = os.path.join(REPO, "tools", "coding_eval")
SAMPLE = os.path.join(TOOL_DIR, "sample.csv")
sys.path.insert(0, TOOL_DIR)

import adapter  # noqa: E402
import metrics  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_file_location("coding_eval_cli", os.path.join(TOOL_DIR, "eval.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


def _u(i, speaker="Pat", text="..."):
    return {"index": i, "speaker_tag": speaker, "text": text}


# --- metrics: single label -----------------------------------------------------
def test_kappa_on_a_hand_computed_2x2_case():
    # Textbook 2x2: both yes 20, both no 15, gold yes/model no 5, gold no/model yes 10.
    # p_o = 35/50 = 0.7; p_e = (25/50)(30/50) + (25/50)(20/50) = 0.5; kappa = 0.2/0.5 = 0.4
    gold = ["y"] * 20 + ["n"] * 15 + ["y"] * 5 + ["n"] * 10
    pred = ["y"] * 20 + ["n"] * 15 + ["n"] * 5 + ["y"] * 10
    assert metrics.accuracy(gold, pred) == pytest.approx(0.7)
    assert metrics.cohen_kappa(gold, pred) == pytest.approx(0.4)
    cm = metrics.confusion_matrix(gold, pred, ["y", "n"])
    assert cm == {"y": {"y": 20, "n": 5}, "n": {"y": 10, "n": 15}}


def test_kappa_edge_cases():
    assert metrics.cohen_kappa(["a", "b", "c"], ["a", "b", "c"]) == pytest.approx(1.0)
    # chance-level: model ignores the item and splits evenly
    assert metrics.cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == pytest.approx(0.0)
    # degenerate: both raters used one label throughout -> defined as 1.0 (all agree)
    assert metrics.cohen_kappa(["a", "a"], ["a", "a"]) == 1.0
    assert metrics.cohen_kappa([], []) == 0.0
    with pytest.raises(ValueError):
        metrics.cohen_kappa(["a"], ["a", "b"])


def test_confusion_keeps_labels_outside_the_codebook():
    cm = metrics.confusion_matrix(["a", "a"], ["a", "zzz"], ["a", "b"])
    assert cm["a"]["<other>"] == 1 and cm["a"]["a"] == 1
    table = metrics.format_confusion(cm)
    assert "gold \\ model" in table and "<other>" in table


# --- metrics: multi label ------------------------------------------------------
def test_multilabel_f1_and_jaccard_by_hand():
    gold = [{"a", "b"}, {"c"}, set()]
    pred = [{"a"}, {"c", "d"}, set()]
    r = metrics.multi_label_report(gold, pred, ["a", "b", "c", "d"])
    # TP: a, c = 2; FP: d = 1; FN: b = 1 -> P = R = F1 = 2/3
    assert r["micro_precision"] == pytest.approx(2 / 3)
    assert r["micro_recall"] == pytest.approx(2 / 3)
    assert r["micro_f1"] == pytest.approx(2 / 3)
    # Jaccard: 1/2, 1/2, and empty-vs-empty counts as 1 -> mean 2/3
    assert r["mean_jaccard"] == pytest.approx(2 / 3)
    assert r["exact_match"] == pytest.approx(1 / 3)
    pl = r["per_label"]
    assert pl["a"] == {"gold": 1, "pred": 1, "tp": 1, "precision": 1.0, "recall": 1.0, "f1": 1.0}
    assert pl["b"]["recall"] == 0.0 and pl["b"]["precision"] == 0.0
    assert pl["d"]["gold"] == 0 and pl["d"]["pred"] == 1 and pl["d"]["f1"] == 0.0
    # macro-F1 averages only labels present in gold: (1 + 0 + 1) / 3
    assert r["macro_f1"] == pytest.approx(2 / 3)
    assert "micro-F1 0.667" in metrics.format_multi_label(r)


def test_jaccard_of_two_empty_sets_is_one():
    assert metrics.jaccard([], []) == 1.0
    assert metrics.jaccard(["x"], []) == 0.0
    assert metrics.jaccard(["x", "y"], ["y", "z"]) == pytest.approx(1 / 3)


# --- gold CSV -------------------------------------------------------------------
def test_load_gold_parses_codes_and_empty_listening(tmp_path):
    p = tmp_path / "g.csv"
    p.write_text(
        "speaker,start_time_s,text,emotion,rip,frame,listening\n"
        'Pat,1.5,"Hi, there",neutral,none,none,\n'
        "Sandy,2,Why?,Neutral,None,none,open_question; ask_why\n"
        "Pat,,Fine.,defusing,interest,future problem-solving,\n",
        encoding="utf-8")
    rows = cli.load_gold(str(p))
    assert [(r["index"], r["speaker_tag"]) for r in rows] == [(0, "Pat"), (1, "Sandy"), (2, "Pat")]
    assert rows[0]["start_time_s"] == 1.5 and rows[2]["start_time_s"] is None
    assert rows[0]["gold"] == {"emotion": "neutral", "rip": "none", "frame": "none", "listening": []}
    assert rows[1]["gold"]["listening"] == ["open_question", "ask_why"]   # trimmed, codebook order
    assert rows[1]["gold"]["emotion"] == "neutral"                        # case-insensitive
    assert rows[2]["gold"]["frame"] == "future_problem_solving"           # spaces / hyphens accepted
    assert rows[0]["team"] == ""                                          # team column optional


def test_load_gold_rejects_bad_codes_and_missing_columns(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("speaker,start_time_s,text,emotion,rip,frame,listening\n"
                 "Pat,1,x,angry,none,none,\n", encoding="utf-8")
    with pytest.raises(cli.GoldError, match="emotion='angry'"):
        cli.load_gold(str(p))
    p.write_text("speaker,start_time_s,text,emotion,rip,frame,listening\n"
                 "Pat,1,x,neutral,none,none,nodding\n", encoding="utf-8")
    with pytest.raises(cli.GoldError, match="listening label 'nodding'"):
        cli.load_gold(str(p))
    p.write_text("speaker,text\nPat,x\n", encoding="utf-8")
    with pytest.raises(cli.GoldError, match="missing column"):
        cli.load_gold(str(p))


def test_codebook_is_the_four_dimensions_the_readme_documents():
    assert adapter.codebook() == {
        "emotion": ["escalating", "defusing", "neutral"],
        "rip": ["interest", "right", "power", "none"],
        "frame": ["past_blame", "future_problem_solving", "none"],
        "listening": ["open_question", "closed_question", "paraphrase", "summarize", "acknowledge",
                      "check_understanding", "ask_why", "ask_priority", "ask_constraint", "interrupt"],
    }
    assert adapter.SINGLE_LABEL == ("emotion", "rip", "frame") and adapter.MULTI_LABEL == ("listening",)


def test_sample_csv_uses_every_code_at_least_once():
    rows = cli.load_gold(SAMPLE)
    assert len(rows) == 20
    assert {r["speaker_tag"] for r in rows} == {"Pat", "Sandy"}
    book = adapter.codebook()
    for dim in adapter.SINGLE_LABEL:
        assert {r["gold"][dim] for r in rows} == set(book[dim]), dim
    seen = {label for r in rows for label in r["gold"]["listening"]}
    assert seen == set(book["listening"])


# --- engine shim: chunking, prompt, request ------------------------------------------
def test_adapter_refuses_to_run_without_the_engine(tmp_path):
    with pytest.raises(ImportError, match="engine not found"):
        adapter._load_engine(str(tmp_path / "nope.py"))


def test_chunks_carry_uncoded_context_and_stay_within_team():
    utts = [dict(_u(i), team="A" if i < 5 else "B") for i in range(8)]
    plan = cli.plan_chunks(utts, size=3, context=2)
    assert [(t, [u["index"] for u in ctx], [u["index"] for u in ch]) for t, ctx, ch in plan] == [
        ("A", [], [0, 1, 2]), ("A", [1, 2], [3, 4]),
        ("B", [], [5, 6, 7]),
    ]
    with pytest.raises(ValueError):
        adapter.chunk_utterances(utts, 0, 1)


def test_prompt_follows_the_llama_server_rules():
    to_code = [_u(3, "Pat", "Why?"), _u(4, "Sandy", "Because.")]
    ctx = [_u(2, "Pat", "Earlier.")]
    msgs = adapter.build_messages(to_code, ctx)
    assert [m["role"] for m in msgs] == ["system", "user"]
    user = msgs[1]["content"]
    assert user.rstrip().endswith("/no_think")
    assert "CONTEXT" in user and "[2] Pat: Earlier." in user
    assert "[3] Pat: Why?" in user and "[4] Sandy: Because." in user
    for dim, labels in adapter.codebook().items():
        for label in labels:
            assert label in msgs[0]["content"], (dim, label)
    # the body the engine's call_llm really sends, captured without network
    body = adapter.request_body(msgs, "m")
    assert body["temperature"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["model"] == "m" and body["messages"] == msgs


# --- parse / repair (the engine's, seen through the shim) ------------------------------
def test_parse_clean_reply():
    reply = ('[{"i": 7, "emotion": "escalating", "rip": "right", "frame": "past_blame", "listening": []}, '
             '{"i": 8, "emotion": "defusing", "rip": "none", "frame": "future_problem_solving", '
             '"listening": ["acknowledge", "closed_question"]}]')
    codes, warnings = adapter.parse_codes(reply, [_u(7), _u(8)])
    assert warnings == []
    assert codes[0] == {"emotion": "escalating", "rip": "right", "frame": "past_blame", "listening": []}
    assert codes[1]["listening"] == ["acknowledge", "closed_question"]


def test_parse_repairs_fences_case_order_and_missing_entries():
    reply = ("Here you go:\n```json\n"
             '[{"i": 1, "emotion": "Defusing", "rip": "Interest", "frame": "Future problem-solving", '
             '"listening": "open_question"}, '
             '{"i": 0, "emotion": "bogus", "rip": "none", "frame": "none", "listening": ["nodding"]}]'
             "\n```")
    codes, warnings = adapter.parse_codes(reply, [_u(0), _u(1), _u(2)])
    assert codes[0] == {"emotion": "neutral", "rip": "none", "frame": "none", "listening": []}
    assert codes[1] == {"emotion": "defusing", "rip": "interest", "frame": "future_problem_solving",
                        "listening": ["open_question"]}
    assert codes[2] == {"emotion": "neutral", "rip": "none", "frame": "none", "listening": []}
    joined = "\n".join(warnings)
    assert "utterance(s) 2 missing" in joined
    assert "2 unknown label(s)" in joined          # 'bogus' and 'nodding'


def test_parse_keeps_whole_objects_of_a_truncated_reply_and_flags_the_rest():
    reply = ('[{"i": 0, "emotion": "neutral", "rip": "none", "frame": "none", "listening": []}, '
             '{"i": 1, "emotion": "escalating", "rip": "pow')
    codes, warnings = adapter.parse_codes(reply, [_u(0), _u(1)])
    assert codes[0]["emotion"] == "neutral"
    assert codes[1] == {"emotion": "neutral", "rip": "none", "frame": "none", "listening": []}
    assert any("1 missing" in w for w in warnings)
    # nothing usable at all -> defaults everywhere, warned
    codes, warnings = adapter.parse_codes("I cannot do that.", [_u(0)])
    assert codes == [{"emotion": "neutral", "rip": "none", "frame": "none", "listening": []}]
    assert any("no JSON array" in w for w in warnings)
    # the model coded a context utterance it was told not to
    _, warnings = adapter.parse_codes('[{"i": 5, "emotion": "neutral", "rip": "none", "frame": "none", '
                                      '"listening": []}, {"i": 9, "emotion": "neutral", "rip": "none", '
                                      '"frame": "none", "listening": []}]', [_u(9)])
    assert any("not in this chunk: 5" in w for w in warnings)


# --- scoring end to end (no LLM) -------------------------------------------------
def test_score_reports_disagreements_per_dimension():
    utts = cli.load_gold(SAMPLE)
    codes = {u["index"]: {k: (list(v) if isinstance(v, list) else v) for k, v in u["gold"].items()} for u in utts}
    codes[3]["emotion"] = "neutral"
    codes[6]["listening"] = ["acknowledge"]
    per_dim, dis = cli.score(utts, codes)
    assert per_dim["rip"]["accuracy"] == 1.0 and per_dim["rip"]["kappa"] == pytest.approx(1.0)
    assert per_dim["emotion"]["accuracy"] == pytest.approx(19 / 20)
    assert per_dim["listening"]["exact_match"] == pytest.approx(19 / 20)
    assert [(d["start_time_s"], d["dimensions"]) for d in dis] == [(25.0, ["emotion"]), (50.0, ["listening"])]
    text = cli.format_report(per_dim, dis, len(utts), ["chunk 1: utterance(s) 4 missing from reply -> defaults"])
    assert "Cohen's kappa" in text and "gold=escalating  model=neutral" in text
    assert "gold=[closed_question;acknowledge]  model=[acknowledge]" in text
    assert "parse warnings (1)" in text


# --- CLI ------------------------------------------------------------------------------
def test_dry_run_prints_prompt_without_network():
    proc = subprocess.run(
        [sys.executable, os.path.join(TOOL_DIR, "eval.py"), "--dry-run", "--gold", SAMPLE,
         "--llm-url", "http://127.0.0.1:9/unreachable"],     # port 9 (discard) would fail if contacted
        capture_output=True, text=True, timeout=60, cwd=REPO)
    assert proc.returncode == 0, proc.stderr
    assert "dry run: 20 utterance(s), 1 chunk(s)" in proc.stdout     # engine chunk size 40 > 20
    assert proc.stdout.count("/no_think") == 1
    assert '"chat_template_kwargs": {"enable_thinking": false}' in proc.stdout
    assert '"temperature": 0' in proc.stdout
    assert "CODE (one object per line)" in proc.stdout
    assert "[0] Sandy:" in proc.stdout and "[19] Pat:" in proc.stdout
    assert "results written" not in proc.stdout


def test_dry_run_chunks_with_context_when_asked():
    proc = subprocess.run(
        [sys.executable, os.path.join(TOOL_DIR, "eval.py"), "--dry-run", "--gold", SAMPLE,
         "--chunk-size", "8", "--context", "2"],
        capture_output=True, text=True, timeout=60, cwd=REPO)
    assert proc.returncode == 0, proc.stderr
    assert "20 utterance(s), 3 chunk(s)" in proc.stdout
    assert proc.stdout.count("/no_think") == 3
    assert proc.stdout.count("CONTEXT (what was said just before") == 2   # chunks 2 and 3 carry context


def test_cli_rejects_bad_gold(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("speaker,text\nPat,x\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, os.path.join(TOOL_DIR, "eval.py"), "--dry-run", "--gold", str(p)],
                          capture_output=True, text=True, timeout=60, cwd=REPO)
    assert proc.returncode == 2 and "missing column" in proc.stderr
