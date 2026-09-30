# coding_eval — does the model code negotiations the way Professor Wang does?

`eval.py` takes a CSV of hand-coded negotiation utterances, has the local LLM
code the same utterances through the production prompt, and reports
agreement per dimension. It is the acceptance test for the "negotiation
coding" feature: hand-code 50–100 lines of a real transcript, run this, and
read the kappas.

The four dimensions and their labels (the codebook, `src/server/negotiation_codebook.json`):

| dimension | type | labels |
|---|---|---|
| `emotion` | one of | `escalating`, `defusing`, `neutral` |
| `rip` | one of | `interest`, `right`, `power`, `none` |
| `frame` | one of | `past_blame`, `future_problem_solving`, `none` |
| `listening` | zero or more | `open_question`, `closed_question`, `paraphrase`, `summarize`, `acknowledge`, `check_understanding`, `ask_why`, `ask_priority`, `ask_constraint`, `interrupt` |

## What it measures

The tool runs the real engine, `src/server/negotiation_coding.py`: its
codebook, chunking (40 utterances per call with the 5 preceding ones shown
read-only), prompt, llama-server call (temperature 0,
`chat_template_kwargs.enable_thinking=false`, trailing `/no_think`) and
parse/repair (`extract_array` + `validate_codes`, so an unknown label becomes
the dimension's default exactly as it would in the database). `adapter.py` is
a thin shim that loads the engine by path and exposes those pieces to
`eval.py` and the tests; it refuses to run without the engine rather than fall
back to a different prompt, because agreement numbers from a different prompt
would be worthless. If the engine renames something, `adapter.py` is the one
file to fix.

## Gold CSV format

One row per utterance, in transcript order, UTF-8, header row required:

| column | required | meaning |
|---|---|---|
| `speaker` | yes | who spoke (`Pat`, `Sandy`, a student id, ...) |
| `start_time_s` | yes (may be blank) | seconds from the start of the recording; used only to label disagreements |
| `text` | yes | the utterance; rows with empty text are skipped |
| `emotion`, `rip`, `frame` | yes | exactly one codebook label each (case-insensitive; spaces/hyphens accepted, e.g. `past blame`) |
| `listening` | yes (may be empty) | zero or more labels separated by `;` (`open_question;ask_why`). Empty means "no listening move", which is the right code for most statements |
| `team` | no | if present, utterances are chunked per team so one prompt never mixes two conversations |

Any label outside the codebook is an error (line number reported), so typos
in hand codes cannot silently become disagreements.

`sample.csv` is a 20-utterance synthetic Viking exchange (Pat and Sandy; the
change order Alex approved while Sandy was on vacation, Pat's cash squeeze,
Sandy's supplier payment due in 15 days, the lakefront lot) with every label
used at least once. Its listening codes follow two conventions that the
codebook does not spell out, and the baseline below shows the model does not
share them, so settle them with Professor Wang before reading her numbers:

- `ask_why` / `ask_priority` / `ask_constraint` are coded *on top of* the
  question's form label (`open_question` or `closed_question`). The model
  codes only the content label.
- `check_understanding` ("have I got that right?") replaces `closed_question`.
- `interrupt` goes on the utterance that cuts in, not the one cut off.

## Running it on the server

Run it from the worktree that has the feature branch checked out (this tool
does not touch the database, Redis or any service; it only POSTs to
llama-server):

```sh
cd /home/vlj9405/code/chemistry-dashboard-pr-negotiation-coding

# 1. See exactly what would be sent (prompt and request settings), without any network call:
src/venv-unified/bin/python tools/coding_eval/eval.py --dry-run --gold tools/coding_eval/sample.csv

# 2. The real thing (llama-server must be up on 127.0.0.1:8080; ~20 s per 20 utterances on the 27B model):
src/venv-unified/bin/python tools/coding_eval/eval.py --gold tools/coding_eval/sample.csv

# Professor Wang's file, results to a named JSON:
src/venv-unified/bin/python tools/coding_eval/eval.py --gold ~/wang-handcoded.csv --out tools/coding_eval/results/wang-$(date +%Y%m%d).json
```

Options: `--llm-url` and `--model` (defaults from the engine:
`http://127.0.0.1:8080/v1/chat/completions`, `qwen3.8-27b-uncensored`, both
overridable with `NEGOTIATION_LLM_URL` / `NEGOTIATION_LLM_MODEL` like
production), `--chunk-size` / `--context` (engine defaults 40 / 5; change them
only to study chunking), `--timeout` (seconds per call, default 300), `--out`
(default `tools/coding_eval/results/<timestamp>.json`; pass `""` to skip).

Do not run it while a class is live: llama-server shares the GPU with the
audio and video services (see `docs/infra-audit-2026-09-30.md`).

The results JSON keeps everything: the metrics, every utterance with its gold
and model codes, the disagreements, the raw model reply per chunk, any parse
warnings and the codebook version, so a run can be re-examined without
re-querying the model.

## Reading the metrics

**Single-label dimensions (`emotion`, `rip`, `frame`)**

- *accuracy* — share of utterances where the model's label equals the hand
  label. Easy to inflate when one label dominates (most utterances are
  `neutral` / `none`), so read it with kappa.
- *Cohen's kappa* — agreement corrected for chance:
  `(p_o − p_e) / (1 − p_e)` where `p_o` is accuracy and `p_e` the agreement
  two raters with these label frequencies would reach by chance. Rough guide
  for coding studies: < 0.4 poor, 0.4–0.6 moderate, 0.6–0.8 substantial,
  > 0.8 near-perfect. Negative means worse than chance.
- *confusion matrix* — rows are the hand label, columns the model's. Read a
  row to see what the model turns a gold label into; the off-diagonal cells
  are the prompt's blind spots.

**Multi-label dimension (`listening`)**

- *per-label precision / recall / F1* — for each listening move: of the
  utterances the model tagged with it, how many the coder also did
  (precision); of the utterances the coder tagged, how many the model caught
  (recall); F1 is their harmonic mean. `gold` and `pred` counts are shown
  because with 50–100 lines many labels will have 1–3 gold instances, where a
  single miss swings F1 from 1.0 to 0.0.
- *micro-F1* — F1 over all (utterance, label) pairs pooled; dominated by the
  frequent labels (`closed_question`, `open_question`). *macro-F1* averages
  F1 over the labels that occur in the gold, so rare labels count equally.
- *mean Jaccard* — per utterance, |gold ∩ model| / |gold ∪ model| (1.0 when
  both are empty), averaged. This is the "how close is each utterance's label
  set" number; it rewards partial matches that exact-set match does not.
- *exact-set match* — share of utterances where the label sets are identical.

**Disagreements** — every utterance where at least one dimension differs,
with time, speaker, text, and `gold=… model=…` per differing dimension. This
list is what to read with Professor Wang: a disagreement is either a model
error, a prompt-definition gap, or a case where the codebook itself is
ambiguous, and only she can say which.

## Tests

```sh
src/venv-unified/bin/python -m pytest tests/test_coding_eval.py -q
```

Pins kappa to a hand-computed 2×2 case (0.4), multi-label F1 / Jaccard on a
tiny fixture, the CSV parser (empty listening, case, bad codes), the engine's
request contract as actually sent (temperature 0, thinking off, `/no_think`;
captured with a stand-in `urlopen`, no network), parse/repair of messy and
truncated replies, that `sample.csv` uses every label, and that `--dry-run`
runs without network.

## Baseline (engine prompt, codebook `viking-v1`, `sample.csv`)

Run 2026-09-30 23:22:19 on the local llama-server, model `qwen3.8-27b-uncensored`,
20 utterances in one chunk, 19 s wall time, reply was clean JSON (no
parse warnings). Full results: `results/20260930-232219.json`. This is a
**synthetic** sample coded by the tool's author, not by Professor Wang; the
number that matters is the one from her real transcript. What it does show is
where the current prompt drifts from the codebook's definitions.

| dimension | accuracy | Cohen's kappa | verdict |
|---|---|---|---|
| emotion | 0.65 (13/20) | 0.27 | poor — the model says `neutral` for most escalating and defusing lines |
| rip | 0.75 (15/20) | 0.65 | substantial |
| frame | 0.65 (13/20) | 0.45 | moderate — over-uses `future_problem_solving` |
| listening | micro-F1 0.85 (P 0.93, R 0.78), macro-F1 0.83, mean Jaccard 0.88, exact-set match 0.75 | — | good precision; misses the form label of content questions and `interrupt` |

14 of 20 utterances differ on at least one dimension; 6 agree on all four.

What the disagreements say:

- **emotion is the weak dimension.** 4 of 5 gold `escalating` and 3 of 4
  gold `defusing` came back `neutral`; the model only calls emotion when the
  wording is overt ("If you'd been reachable, none of this would have
  happened" was caught; "I could just let you sue me ... My lawyer says Alex's
  signature won't hold" and the lien counter-threat were not; neither was the
  settlement proposal nor "So what would actually work for you?"). The
  codebook's `escalating` definition lists tone words (counter-anger,
  sarcasm, irritation); threats and hard-line positions delivered calmly, and
  conciliatory moves without feeling words, fall through. Nothing was coded
  in the wrong non-neutral direction, so this is a recall problem the
  definitions can fix.
- **frame**: 5 of 11 gold `none` rows became `future_problem_solving`: the
  model treats any talk of deadlines, consequences or leverage as problem
  solving ("Is there any flexibility on that date?", "I could let you sue
  me", the lien threat). The closing summary was coded `none` and Pat's
  contract line `none` instead of `past_blame`. Some of these Professor Wang
  may side with the model on.
- **rip**: 5 misses. Three gold `none` questions were coded `interest`
  (asking *why* the bank pulled the line, asking about flexibility, asking
  what matters most): the codebook's example "What do you need the cash
  for?" is itself a question, so the model reasonably codes questions about
  the other side's needs as `interest`; the gold treats pure questions as
  `none`. Decide which convention holds. The other two: a paraphrase of
  Pat's contract argument coded `right`, and "that would sink me faster
  than it sinks you" coded `power`.
- **listening**: precision 0.93, recall 0.78. Both `open_question` and
  `closed_question` misses are the form-label convention above (the model
  returned only `ask_why` / `ask_constraint`); `interrupt` was missed on
  "The lakefront lot? You want the lot?" although the previous utterance
  trails off with "..."; and "No. We don't." was tagged `acknowledge`. The
  acknowledge line's embedded "Can we set aside ... ?" was not tagged
  `closed_question`.

Verbatim report of the run (rows are gold, columns are the model):

```
== emotion ==  accuracy 0.650 (13/20)  Cohen's kappa 0.275
gold \ model      escalating      defusing       neutral
escalating                 1             0             4
defusing                   0             1             3
neutral                    0             0            11

== rip ==  accuracy 0.750 (15/20)  Cohen's kappa 0.654
gold \ model        interest         right         power          none
interest                   5             0             1             0
right                      0             3             0             0
power                      0             0             2             0
none                       3             1             0             5

== frame ==  accuracy 0.650 (13/20)  Cohen's kappa 0.451
gold \ model                          past_blame  future_problem_solving                    none
past_blame                                     3                       0                       1
future_problem_solving                         0                       4                       1
none                                           0                       5                       6

== listening (multi-label) ==
label                 gold  pred   tp      P      R     F1
open_question            3     2    2   1.00   0.67   0.80
closed_question          6     4    4   1.00   0.67   0.80
paraphrase               2     2    2   1.00   1.00   1.00
summarize                1     1    1   1.00   1.00   1.00
acknowledge              1     2    1   0.50   1.00   0.67
check_understanding      1     1    1   1.00   1.00   1.00
ask_why                  1     1    1   1.00   1.00   1.00
ask_priority             1     1    1   1.00   1.00   1.00
ask_constraint           1     1    1   1.00   1.00   1.00
interrupt                1     0    0   0.00   0.00   0.00
micro-F1 0.848 (P 0.933 R 0.778)  macro-F1 0.827  mean Jaccard 0.883  exact-set match 0.750

== disagreements: 14 of 20 utterances differ on at least one dimension (6 fully agree) ==
t=    8.5s Pat      Honestly? I'm looking at an invoice for a hundred and eighty thousand dollars for work ...
    emotion    gold=escalating  model=neutral
t=   18.0s Sandy    So your position is that the change order isn't valid because you personally didn't sig...
    rip        gold=none  model=right
t=   25.0s Pat      Alex had no authority to approve a change of that size. Read the contract: anything ove...
    emotion    gold=escalating  model=neutral
    frame      gold=past_blame  model=none
t=   50.0s Sandy    I hear that you're frustrated, and I'd be too. Can we set aside who should have been re...
    listening  gold=[closed_question;acknowledge]  model=[acknowledge]
t=   72.0s Sandy    Why did the bank pull the line?
    rip        gold=none  model=interest
    listening  gold=[open_question;ask_why]  model=[ask_why]
t=   83.0s Sandy    Okay, so cash is tight for at least this quarter. Here's my constraint: I have a suppli...
    frame      gold=none  model=future_problem_solving
t=   95.0s Pat      Is there any flexibility on that date, or is fifteen days hard?
    rip        gold=none  model=interest
    frame      gold=none  model=future_problem_solving
    listening  gold=[closed_question;ask_constraint]  model=[ask_constraint]
t=  100.0s Sandy    It's hard. Miss it and they put me on cash-on-delivery for every job I have running. Th...
    rip        gold=interest  model=power
    frame      gold=none  model=future_problem_solving
t=  110.0s Pat      Look, I could just let you sue me over the change order and tie it up for two years. My...
    emotion    gold=escalating  model=neutral
    frame      gold=none  model=future_problem_solving
t=  120.0s Sandy    You could. And I could file a lien on the building tomorrow, and your other tenants wou...
    emotion    gold=escalating  model=neutral
    frame      gold=none  model=future_problem_solving
t=  132.0s Pat      No. We don't. So what would actually work for you? What matters most: getting the full ...
    emotion    gold=defusing  model=neutral
    rip        gold=none  model=interest
    listening  gold=[open_question;ask_priority]  model=[acknowledge;open_question;ask_priority]
t=  152.0s Pat      The lakefront lot? You want the lot?
    listening  gold=[closed_question;interrupt]  model=[closed_question]
t=  156.0s Sandy    I've wanted it for years, you know that. Say you pay ninety in cash within fifteen days...
    emotion    gold=defusing  model=neutral
t=  170.0s Pat      So, to make sure I've got this: ninety in cash in fifteen days, which I can stretch to;...
    emotion    gold=defusing  model=neutral
    frame      gold=future_problem_solving  model=none
```
