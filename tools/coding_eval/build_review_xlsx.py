"""Build the reviewer workbook for Prof. Wang from the coding-eval results.

Usage: build_review_xlsx.py --out <file.xlsx> [--results tools/coding_eval/results]
       [--scenarios tools/coding_eval/scenarios] [--sample tools/coding_eval/sample.csv]

For every scenario CSV (sample.csv + scenarios/*.csv) that has a results/<stem>.json
(or viking-v2.json for the sample), one sheet with the coded transcript (our codes
beside the model's), an Agree? formula, a "Your call" column for the reviewer, the
per-team roll-up as COUNTIFS formulas over the sheet, and the engine's timeline and
opening-move summary. Plus: Read me, Scheme, Summary (agreement per scenario and
pooled), Conventions, and a hand-coding Template with dropdowns.
"""
import argparse, csv, glob, json, os, sys
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src", "server"))
sys.path.insert(0, HERE)
import negotiation_coding as engine  # noqa: E402
import metrics as M  # noqa: E402

FONT = "Arial"
F = Font(name=FONT, size=10)
FB = Font(name=FONT, size=10, bold=True)
FH = Font(name=FONT, size=14, bold=True)
FI = Font(name=FONT, size=10, italic=True, color="555555")
INPUT = PatternFill("solid", fgColor="FFFF00")
HEAD = PatternFill("solid", fgColor="DDEBF7")
NO = PatternFill("solid", fgColor="FCE4D6")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
DIMS = ("emotion", "rip", "frame", "listening")
SITUATIONS = [  # (stem prefix, number, title, what the scenario is for)
    ("instructed_anger", 1, "Instructed anger",
     "Sandy's side plays angry for the first ten minutes, as instructed in class; Pat's side tries to de-escalate. Tests escalation recall and the first sustained de-escalation."),
    ("rights_debate", 2, "Rights debate",
     "Both sides argue contract clauses and precedent and never reach interests. Tests right vs none, and past_blame on dry rebuttals."),
    ("power_spiral", 3, "Power spiral",
     "Threats and counter-threats, a wise if-then threat, and a late rescue. Tests power, and whether a calmly worded threat counts as escalating."),
    ("interest_based", 4, "Interest-based",
     "A well-run negotiation: brief rights, then interests, many listening moves, three packages, a deal. Tests interest, and defusing on proposals and summaries."),
    ("messy_realistic", 5, "Messy, realistic speech",
     "Fragments, overlaps, jokes, numbers read aloud, side comments: closer to what classroom recordings look like."),
    ("mixed_team_dynamics", 6, "Mixed team dynamics",
     "One teammate blames while another steers; within-team asides and interruptions of a teammate."),
]


def scenario_title(stem):
    """('Scenario 3b: Power spiral', blurb, sort key) for scenarios/<stem>.csv."""
    for prefix, num, title, blurb in SITUATIONS:
        if stem == prefix or stem.startswith(prefix + "_"):
            variant = stem[len(prefix) + 1:] or "a"
            return "Scenario %d%s: %s" % (num, variant, title), blurb + " (script %s of 2)" % variant, (num, variant)
    return stem.replace("_", " ").capitalize(), "", (99, stem)


def mmss(s):
    if s is None:
        return ""
    s = int(round(float(s)))
    return "%d:%02d" % (s // 60, s % 60)


def lst(v):
    if not v:
        return ""
    return ";".join(sorted(v)) if isinstance(v, list) else str(v)


def style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = FB; cell.fill = HEAD; cell.border = BOX; cell.alignment = WRAP


def sheet_readme(wb, scenarios, asof):
    ws = wb.active; ws.title = "Read me"
    lines = [
        ("Viking negotiation coding review", FH),
        ("Prepared %s by the BLINC team for Prof. Cynthia Wang." % asof, F),
        ("", F),
        ("What this is", FB),
        ("BLINC codes every utterance of a negotiation on four dimensions (emotion, interests/rights/power, past-blame vs future problem-solving, listening moves) "
         "using your definitions from the Class 4 decks, then rolls the codes up per team and builds a de-escalation timeline. "
         "This workbook shows what that produces on %d scripted scenarios so you can judge whether the scheme is applied the way you intend." % len(scenarios), F),
        ("", F),
        ("The situations (two independently written scripts of each, a and b): " + "; ".join("%d. %s" % (n, t) for _, n, t, _ in SITUATIONS) + ". Scenario 0 is the original 20-line sample.", F),
        ("", F),
        ("How to read it", FB),
        ("Scheme: the codes and the decision rule the model is given for each.", F),
        ("Summary: how closely the model matched our own hand codes, per scenario and pooled (Cohen's kappa for single-label dimensions, F1 for listening).", F),
        ("One sheet per scenario: the transcript with our codes and the model's side by side; Agree? is computed; the yellow 'Your call' column is for you. Below each transcript: the roll-up per team (formulas over the sheet) and the timeline and opening moves computed from the model's codes.", F),
        ("Conventions: borderline cases where a reasonable coder could go either way; the yellow column asks for your decision.", F),
        ("Template: a sheet to hand-code 50 to 100 lines of a real negotiation; yellow cells are inputs and the code columns have dropdowns. Send it back and we measure agreement against you rather than against us.", F),
        ("", F),
        ("Caveats", FB),
        ("The scenarios are scripted and cleaner than classroom speech (complete sentences, one move per line, no overlap). The 'our codes' column is the BLINC team's reading of your scheme, not yours. Kappa and F1 values were computed by the evaluation tool; counts in the roll-ups are live formulas.", F),
        ("Legend: yellow = cells for you to fill; blue = model output; light-red Agree? cells mark disagreements.", FI),
    ]
    for i, (t, f) in enumerate(lines, start=1):
        c = ws.cell(row=i, column=1, value=t); c.font = f; c.alignment = WRAP
    ws.column_dimensions["A"].width = 120


def sheet_scheme(wb, codebook):
    ws = wb.create_sheet("Scheme")
    ws.append(["Dimension", "Code", "Decision rule", "Example phrases"]); style_header(ws, 1, 4)
    for dim, spec in codebook["dimensions"].items():
        for code, c in spec["codes"].items():
            rule = c.get("rule") or c.get("definition") or c.get("description") or ""
            ex = " | ".join('"%s"' % e for e in (c.get("examples") or []))
            ws.append([dim, code, rule, ex])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = F; cell.alignment = WRAP; cell.border = BOX
    for col, w in zip("ABCD", (12, 24, 80, 70)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    r = ws.max_row + 2
    ws.cell(row=r, column=1, value="Case facts given to the model:").font = FB
    for i, fact in enumerate(codebook.get("case_facts") or [], start=1):
        c = ws.cell(row=r + i, column=1, value="- " + fact); c.font = F; c.alignment = WRAP
        ws.merge_cells(start_row=r + i, start_column=1, end_row=r + i, end_column=4)


def load_scenario(csv_path, json_path):
    gold = list(csv.DictReader(open(csv_path)))
    res = json.load(open(json_path))
    by_index = {u["index"]: u for u in res["utterances"]}
    rows = []
    for i, g in enumerate(gold):
        u = by_index.get(i, {})
        rows.append({
            "index": i, "time": float(g.get("start_time_s") or 0), "speaker": g["speaker"],
            "team": (g.get("team") if g.get("team") in ("Pat", "Sandy") else (g["speaker"] if g["speaker"] in ("Pat", "Sandy") else (g.get("team") or ""))), "text": g["text"],
            "gold": {"emotion": g["emotion"], "rip": g["rip"], "frame": g["frame"],
                     "listening": [x for x in (g.get("listening") or "").split(";") if x]},
            "model": u.get("model") or {},
        })
    return rows, res


def engine_codes(rows):
    codes, teams = [], {}
    for r in rows:
        m = r["model"] or {}
        codes.append({"transcript_id": r["index"], "start_time": r["time"], "length": 0,
                      "speaker_tag": r["speaker"], "emotion": m.get("emotion"), "rip": m.get("rip"),
                      "frame": m.get("frame"), "listening": m.get("listening") or []})
        # The team column names Pat or Sandy; the original sample tags a session
        # instead, so fall back to the speaker's own name when it is a team name.
        team = r["team"] if r["team"] in engine.TEAMS else (r["speaker"] if r["speaker"] in engine.TEAMS else None)
        if team:
            teams[r["speaker"]] = team
    return codes, teams


def sheet_scenario(wb, name, title, blurb, rows, res, codebook):
    ws = wb.create_sheet(name[:31])
    ws.cell(row=1, column=1, value=title).font = FH
    ws.cell(row=2, column=1, value=(blurb + " " if blurb else "") + "Our hand codes beside the model's; the yellow column is yours.").font = FI
    hdr = ["#", "Time", "Speaker", "Team", "Utterance",
           "Our emotion", "Our RIP", "Our frame", "Our listening",
           "Model emotion", "Model RIP", "Model frame", "Model listening",
           "Agree?", "Your call (if you would code it differently)", "Notes"]
    ws.append([]); ws.append(hdr); H = 4; style_header(ws, H, len(hdr))
    first = H + 1
    for r in rows:
        g, m = r["gold"], r["model"]
        ws.append([r["index"] + 1, mmss(r["time"]), r["speaker"], r["team"], r["text"],
                   g["emotion"], g["rip"], g["frame"], lst(g["listening"]),
                   m.get("emotion", ""), m.get("rip", ""), m.get("frame", ""), lst(m.get("listening")),
                   None, None, None])
        rr = ws.max_row
        ws.cell(row=rr, column=14, value='=IF(AND(F%d=J%d,G%d=K%d,H%d=L%d,I%d=M%d),"yes","no")' % ((rr,) * 8))
        for c in range(1, len(hdr) + 1):
            cell = ws.cell(row=rr, column=c); cell.font = F; cell.border = BOX; cell.alignment = WRAP
        for c in (10, 11, 12, 13):
            ws.cell(row=rr, column=c).font = Font(name=FONT, size=10, color="1F4E79")
        ws.cell(row=rr, column=15).fill = INPUT
        if not (g["emotion"] == m.get("emotion") and g["rip"] == m.get("rip") and g["frame"] == m.get("frame")
                and sorted(g["listening"]) == sorted(m.get("listening") or [])):
            ws.cell(row=rr, column=14).fill = NO
    last = ws.max_row
    widths = [4, 6, 10, 7, 70, 12, 10, 20, 26, 12, 10, 20, 26, 8, 28, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=first, column=6)

    # Roll-up per team from the MODEL columns (live formulas).
    r0 = last + 3
    ws.cell(row=r0, column=1, value="Roll-up by team (model codes; counts are formulas over the table above)").font = FB
    ws.append([]); hrow = r0 + 1
    for c, v in enumerate(["Dimension", "Code", "Pat", "Sandy", "Pat %", "Sandy %"], start=1):
        ws.cell(row=hrow, column=c, value=v)
    style_header(ws, hrow, 6)
    col_for = {"emotion": "J", "rip": "K", "frame": "L", "listening": "M"}
    nrow = hrow
    tot_row_formula = {t: 'COUNTIF($D$%d:$D$%d,"%s")' % (first, last, t) for t in ("Pat", "Sandy")}
    for dim, spec in codebook["dimensions"].items():
        for code in spec["codes"]:
            nrow += 1
            ws.cell(row=nrow, column=1, value=dim); ws.cell(row=nrow, column=2, value=code)
            crit = '"*%s*"' % code if dim == "listening" else '"%s"' % code
            for ci, team in ((3, "Pat"), (4, "Sandy")):
                ws.cell(row=nrow, column=ci, value='=COUNTIFS($D$%d:$D$%d,"%s",$%s$%d:$%s$%d,%s)'
                        % (first, last, team, col_for[dim], first, col_for[dim], last, crit))
            for ci, team in ((5, "Pat"), (6, "Sandy")):
                cnt = get_column_letter(ci - 2)
                ws.cell(row=nrow, column=ci, value='=IF(%s=0,0,%s%d/%s)' % (tot_row_formula[team], cnt, nrow, tot_row_formula[team]))
                ws.cell(row=nrow, column=ci).number_format = "0%"
            for c in range(1, 7):
                ws.cell(row=nrow, column=c).font = F; ws.cell(row=nrow, column=c).border = BOX
    nrow += 1
    ws.cell(row=nrow, column=2, value="utterances").font = FB
    ws.cell(row=nrow, column=3, value="=" + tot_row_formula["Pat"]); ws.cell(row=nrow, column=4, value="=" + tot_row_formula["Sandy"])

    # Timeline + opening moves from the engine, computed from the model codes.
    codes, teams = engine_codes(rows)
    summary = engine.summarize(codes, teams, codebook)
    tl = summary.get("timeline") or {}
    r1 = nrow + 3
    ws.cell(row=r1, column=1, value="Timeline (model codes; minutes:seconds from the start)").font = FB
    labels = [("first_escalating", "First escalating statement"),
              ("peak_escalation_window_start", "Peak: start of the 60 s window with most escalation"),
              ("first_sustained_deescalation", "First sustained de-escalation (2 min with no escalation after the first)"),
              ("first_future_move", "First future-focused move")]
    for i, (k, label) in enumerate(labels, start=1):
        v = tl.get(k, tl.get(k + "_s"))
        ws.cell(row=r1 + i, column=1, value=label).font = F
        ws.cell(row=r1 + i, column=3, value=mmss(v) if v is not None else "never").font = F
    dyn = engine.team_dynamics(codes, teams)
    r2 = r1 + len(labels) + 2
    ws.cell(row=r2, column=1, value="Opening move and RIP transitions per team (model codes)").font = FB
    for c, v in enumerate(["Team", "Opening move (first 3 min)", "RIP sequence", "Last escalating line", "First interest question"], start=1):
        ws.cell(row=r2 + 1, column=c, value=v)
    style_header(ws, r2 + 1, 5)
    for i, team in enumerate(("Pat", "Sandy"), start=2):
        d = dyn.get(team) or {}
        ws.cell(row=r2 + i, column=1, value=team)
        ws.cell(row=r2 + i, column=2, value=d.get("opening_move") or d.get("opening") or "")
        seq = d.get("rip_sequence") or d.get("transitions") or []
        ws.cell(row=r2 + i, column=3, value=" -> ".join(seq) if isinstance(seq, list) else str(seq))
        ws.cell(row=r2 + i, column=4, value=mmss(d.get("last_escalating")) if d.get("last_escalating") is not None else "never")
        fq = d.get("first_interest_question")
        ws.cell(row=r2 + i, column=5, value=mmss(fq) if fq is not None else "never")
        for c in range(1, 6):
            ws.cell(row=r2 + i, column=c).font = F; ws.cell(row=r2 + i, column=c).border = BOX
    return first, last


def sheet_summary(wb, entries, codebook_labels):
    ws = wb.create_sheet("Summary", 2)
    ws.cell(row=1, column=1, value="Agreement between our hand codes and the model").font = FH
    ws.cell(row=2, column=1, value="Kappa: 0 = chance, 1 = perfect (0.4-0.6 moderate, 0.6-0.8 substantial). Listening F1: share of listening codes matched. Lines and Disagreements are formulas over each scenario sheet; kappa and F1 were computed by the evaluation tool.").font = FI
    hdr = ["Scenario", "Lines", "Disagreements", "Emotion kappa", "RIP kappa", "Frame kappa", "Listening micro-F1", "Exact-set match (listening)"]
    ws.append([]); ws.append(hdr); style_header(ws, 4, len(hdr))
    pooled = {d: ([], []) for d in DIMS}
    for e in entries:
        m = e["res"]["metrics"]; sheet = "'%s'" % e["sheet"]
        lis = m.get("listening", {})
        ws.append([e["title"],
                   "=COUNTA(%s!$E$%d:$E$%d)" % (sheet, e["first"], e["last"]),
                   '=COUNTIF(%s!$N$%d:$N$%d,"no")' % (sheet, e["first"], e["last"]),
                   round(m["emotion"]["kappa"], 2), round(m["rip"]["kappa"], 2), round(m["frame"]["kappa"], 2),
                   round(lis.get("micro_f1", lis.get("micro", {}).get("f1", 0)), 2),
                   round(lis.get("exact_match", 0), 2)])
        for r in e["rows"]:
            for d in ("emotion", "rip", "frame"):
                pooled[d][0].append(r["gold"][d]); pooled[d][1].append((r["model"] or {}).get(d))
            pooled["listening"][0].append(set(r["gold"]["listening"])); pooled["listening"][1].append(set((r["model"] or {}).get("listening") or []))
    n = sum(len(e["rows"]) for e in entries)
    labels = list(codebook_labels)
    ml = M.multi_label_report(pooled["listening"][0], pooled["listening"][1], labels)
    ws.append(["Pooled (all scenarios)", n, sum(1 for e in entries for r in e["rows"]
               if not (r["gold"]["emotion"] == (r["model"] or {}).get("emotion") and r["gold"]["rip"] == (r["model"] or {}).get("rip")
                       and r["gold"]["frame"] == (r["model"] or {}).get("frame") and sorted(r["gold"]["listening"]) == sorted((r["model"] or {}).get("listening") or []))),
               round(M.cohen_kappa(*pooled["emotion"]), 2), round(M.cohen_kappa(*pooled["rip"]), 2), round(M.cohen_kappa(*pooled["frame"]), 2),
               round(ml.get("micro_f1", 0), 2) if isinstance(ml, dict) else "", round(ml.get("exact_match", 0), 2) if isinstance(ml, dict) else ""])
    for row in ws.iter_rows(min_row=5):
        for cell in row:
            cell.font = F; cell.border = BOX
    for c in range(1, 9):
        ws.cell(row=ws.max_row, column=c).font = FB
    for col, w in zip("ABCDEFGH", (34, 8, 14, 14, 10, 12, 18, 24)):
        ws.column_dimensions[col].width = w


def sheet_conventions(wb):
    ws = wb.create_sheet("Conventions")
    ws.cell(row=1, column=1, value="Conventions to settle: where a reasonable coder could go either way").font = FH
    hdr = ["Case", "Example line", "Options", "What we currently do", "Your decision"]
    ws.append([]); ws.append(hdr); style_header(ws, 3, 5)
    rows = [
        ("A question about the other side's needs", "What do you need the cash for?", "RIP = interest, or none because it is a question", "interest", ""),
        ("A calm-toned settlement proposal", "Say you pay ninety in cash within fifteen days and we settle the rest against the lot.", "Emotion = defusing (moves toward resolution) or neutral", "defusing", ""),
        ("A conditional threat stated politely", "If you do not pay by Friday, then I will file for bankruptcy.", "Emotion = escalating or neutral; RIP = power either way", "escalating", ""),
        ("Blame with no standard invoked", "If you had been reachable, none of this would have happened.", "RIP = none, or right (an implied norm); frame = past_blame either way", "none", ""),
        ("Sarcasm that reads calm on paper", "Well, that is very generous of you.", "The model only sees words; under-call or infer tone?", "under-call", ""),
        ("Interrupting a teammate vs the other side", "A note taker cutting in with a figure", "interrupt for the counterpart only, or anyone", "anyone", ""),
        ("Questions that are also listening moves", "Why does it have to be this week?", "ask_why alone, or ask_why plus open_question", "both", ""),
        ("A dry rebuttal or impasse statement in a legal argument", "An email is not a signed change order. / We're going in circles. Our position stands.", "Emotion = neutral (no heat in the words) or escalating (a hard line with an edge); the model often says escalating", "neutral", ""),
        ("Past facts stated without blame", "Pat was unreachable for two weeks. / Fawn sent the email in March.", "Frame = none, or past_blame because it is about who did what; the model is split", "none", ""),
        ("A confirmation or question about a proposal on the table", "And you'd put that in writing today? / Three works if the money lands by the fifteenth.", "Frame = future_problem_solving (it advances the deal) or none (not itself a proposal)", "future_problem_solving", ""),
        ("Proposals, summaries and acknowledgements that mention needs or money", "So the gap is three sixty, not four hundred. / One million this week, the rest against spring work.", "RIP = none (no standard invoked) or interest (it is about needs); the model often says interest", "none", ""),
        ("A closing summary or 'I'll put it in writing' concession", "That's it. Let's write it down. / Agreed. We'll have it drafted by tonight.", "Emotion = defusing (moves toward resolution) or neutral; the model usually says neutral", "defusing", ""),
        ("Rhetorical questions and questions to one's own teammate", "Is that the plan? / (to Morgan) do we have the escrow number", "A listening move (closed_question) or not coded, since it is not directed at the other side", "not coded", ""),
    ]
    for r in rows:
        ws.append(list(r))
        rr = ws.max_row
        for c in range(1, 6):
            cell = ws.cell(row=rr, column=c); cell.font = F; cell.border = BOX; cell.alignment = WRAP
        ws.cell(row=rr, column=5).fill = INPUT
    for col, w in zip("ABCDE", (34, 50, 44, 20, 30)):
        ws.column_dimensions[col].width = w


def sheet_template(wb, codebook):
    ws = wb.create_sheet("Template (hand-code)")
    ws.cell(row=1, column=1, value="Hand-coding template: fill the yellow cells for 50 to 100 lines of a real negotiation").font = FH
    ws.cell(row=2, column=1, value="Legend: yellow = your input. emotion, rip, frame and team have dropdowns; listening takes one or more codes separated by semicolons (e.g. open_question;ask_why) or stays blank. Row 5 is an example; replace it.").font = FI
    hdr = ["speaker", "start_time_s", "text", "emotion", "rip", "frame", "listening", "team"]
    ws.append([]); ws.append(hdr); style_header(ws, 4, 8)
    ws.append(["Sandy", 50, "I hear that you're frustrated, and I'd be too. Can we set aside who should have been reachable and look at what we do now?",
               "defusing", "none", "future_problem_solving", "acknowledge;closed_question", "Sandy"])
    for r in range(5, 106):
        for c in range(1, 9):
            cell = ws.cell(row=r, column=c); cell.fill = INPUT; cell.font = F; cell.border = BOX; cell.alignment = WRAP
    dims = codebook["dimensions"]
    for col, dim in (("D", "emotion"), ("E", "rip"), ("F", "frame")):
        dv = DataValidation(type="list", formula1='"%s"' % ",".join(dims[dim]["codes"].keys()), allow_blank=True)
        ws.add_data_validation(dv); dv.add("%s5:%s105" % (col, col))
    dv = DataValidation(type="list", formula1='"Pat,Sandy"', allow_blank=True); ws.add_data_validation(dv); dv.add("H5:H105")
    ws.cell(row=107, column=1, value="Listening codes: " + ", ".join(dims["listening"]["codes"].keys())).font = FI
    for col, w in zip("ABCDEFGH", (12, 12, 80, 14, 10, 24, 30, 8)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A5"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--results", default=os.path.join(HERE, "results"))
    ap.add_argument("--scenarios", default=os.path.join(HERE, "scenarios"))
    ap.add_argument("--sample", default=os.path.join(HERE, "sample.csv"))
    ap.add_argument("--asof", default="2026-10-01")
    a = ap.parse_args()
    codebook = engine.load_codebook()
    entries = []
    csvs = [a.sample] + sorted(glob.glob(os.path.join(a.scenarios, "*.csv")))
    for p in csvs:
        stem = os.path.splitext(os.path.basename(p))[0]
        j = os.path.join(a.results, stem + ".json")
        if stem == "sample" and not os.path.exists(j):
            j = os.path.join(a.results, "viking-v2.json")
        if not os.path.exists(j):
            print("skip (no results):", stem); continue
        rows, res = load_scenario(p, j)
        if stem == "sample":
            title, blurb, key = "Scenario 0: opening exchange (original sample)", "The first 20-line sample the tool was built on.", (0, "")
        else:
            title, blurb, key = scenario_title(stem)
        entries.append({"stem": stem, "sheet": stem[:31], "title": title, "blurb": blurb, "key": key, "rows": rows, "res": res})
    entries.sort(key=lambda e: e["key"])
    wb = Workbook()
    sheet_readme(wb, entries, a.asof); sheet_scheme(wb, codebook)
    for e in entries:
        e["first"], e["last"] = sheet_scenario(wb, e["sheet"], e["title"], e["blurb"], e["rows"], e["res"], codebook)
    sheet_summary(wb, entries, codebook["dimensions"]["listening"]["codes"].keys()); sheet_conventions(wb); sheet_template(wb, codebook)
    wb.save(a.out); print("wrote", a.out, "with", len(entries), "scenario sheets")


if __name__ == "__main__":
    main()
