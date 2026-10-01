"""Code every scenario (and sample.csv) with the live model and print one table.

Usage: run_all.py [--scenarios tools/coding_eval/scenarios] [--results tools/coding_eval/results]
                  [--only stem[,stem...]] [--skip-existing] [--llm-url URL] [--model NAME]

For each CSV it runs eval.py's pipeline (same prompt, chunking and parsing as the
engine) and writes results/<stem>.json, then prints per-scenario kappa / F1 and a
pooled row computed over every utterance of every scenario. Exit status is 1 if
any scenario failed or any reply needed repair (warnings), so a cron or CI run
notices. llama-server must be up; ~30 s per 30-line scenario on the 27B model.
"""
import argparse, glob, json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import eval as cli  # noqa: E402
import metrics as M  # noqa: E402
import adapter  # noqa: E402


def pooled(results):
    gold = {d: [] for d in ("emotion", "rip", "frame")}; pred = {d: [] for d in gold}
    gl, pl, n = [], [], 0
    for r in results:
        for u in r["utterances"]:
            n += 1
            for d in gold:
                gold[d].append(u["gold"][d]); pred[d].append(u["model"].get(d))
            gl.append(set(u["gold"]["listening"])); pl.append(set(u["model"].get("listening") or []))
    labels = list(adapter.CODEBOOK["dimensions"]["listening"]["codes"].keys())
    ml = M.multi_label_report(gl, pl, labels)
    return {"n": n, "emotion": M.cohen_kappa(gold["emotion"], pred["emotion"]), "rip": M.cohen_kappa(gold["rip"], pred["rip"]),
            "frame": M.cohen_kappa(gold["frame"], pred["frame"]), "micro_f1": ml["micro_f1"], "exact_match": ml["exact_match"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scenarios", default=os.path.join(HERE, "scenarios"))
    ap.add_argument("--results", default=os.path.join(HERE, "results"))
    ap.add_argument("--sample", default=os.path.join(HERE, "sample.csv"))
    ap.add_argument("--only", default="", help="comma-separated stems to run")
    ap.add_argument("--skip-existing", action="store_true", help="reuse results/<stem>.json when present")
    ap.add_argument("--llm-url", default=None); ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=float, default=None)
    a = ap.parse_args(argv)
    os.makedirs(a.results, exist_ok=True)
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    paths = [a.sample] + sorted(glob.glob(os.path.join(a.scenarios, "*.csv")))
    rows, results, failed = [], [], 0
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        if only and stem not in only:
            continue
        out = os.path.join(a.results, stem + ".json")
        t0 = time.time()
        if a.skip_existing and os.path.exists(out):
            res = json.load(open(out)); src = "cached"
        else:
            args = ["--gold", p, "--out", out]
            if a.llm_url: args += ["--llm-url", a.llm_url]
            if a.model: args += ["--model", a.model]
            if a.timeout: args += ["--timeout", str(a.timeout)]
            try:
                rc = cli.main(args)
            except Exception as e:  # keep going; report at the end
                print("%-24s FAILED: %s" % (stem, e), file=sys.stderr); failed += 1; continue
            if rc not in (0, None) or not os.path.exists(out):
                print("%-24s FAILED (exit %s)" % (stem, rc), file=sys.stderr); failed += 1; continue
            res = json.load(open(out)); src = "%.0f s" % (time.time() - t0)
        m = res["metrics"]; lis = m["listening"]
        results.append(res)
        rows.append((stem, len(res["utterances"]), m["emotion"]["kappa"], m["rip"]["kappa"], m["frame"]["kappa"],
                     lis["micro_f1"], lis["exact_match"], len(res.get("warnings") or []), src))
    print("\n%-24s %5s %8s %8s %8s %8s %8s %6s  %s" % ("scenario", "n", "emo k", "rip k", "frame k", "lis F1", "exact", "warns", "run"))
    for r in rows:
        print("%-24s %5d %8.2f %8.2f %8.2f %8.2f %8.2f %6d  %s" % r)
    if results:
        p = pooled(results)
        print("%-24s %5d %8.2f %8.2f %8.2f %8.2f %8.2f" % ("POOLED", p["n"], p["emotion"], p["rip"], p["frame"], p["micro_f1"], p["exact_match"]))
        json.dump({"scenarios": [dict(zip(("stem", "n", "emotion_kappa", "rip_kappa", "frame_kappa", "listening_micro_f1",
                                            "listening_exact_match", "warnings", "run"), r)) for r in rows], "pooled": p},
                  open(os.path.join(a.results, "summary.json"), "w"), indent=1)
    warned = sum(r[7] for r in rows)
    return 1 if (failed or warned) else 0


if __name__ == "__main__":
    sys.exit(main())
