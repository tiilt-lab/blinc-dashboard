"""Agreement metrics for the coding evaluation (stdlib only).

Single-label dimensions: accuracy, Cohen's kappa, confusion matrix.
Multi-label dimension (listening): per-label precision / recall / F1, micro-F1,
macro-F1, mean Jaccard and exact-set match.

Conventions: precision with no predictions of a label is 0, recall with no
gold instances is 0 (both flagged by the counts in the table); Jaccard of two
empty sets is 1 (the model correctly said "no listening move").
"""
from collections import Counter


# --- single label -------------------------------------------------------------
def accuracy(gold, pred):
    if len(gold) != len(pred):
        raise ValueError("gold and pred differ in length")
    if not gold:
        return 0.0
    return sum(1 for g, p in zip(gold, pred) if g == p) / len(gold)


def cohen_kappa(gold, pred):
    """Cohen's kappa for two raters over the same items.

    kappa = (p_o - p_e) / (1 - p_e); when p_e == 1 (both raters used a single
    identical label throughout) the statistic is undefined and we return 1.0
    if they agreed everywhere, else 0.0.
    """
    if len(gold) != len(pred):
        raise ValueError("gold and pred differ in length")
    n = len(gold)
    if n == 0:
        return 0.0
    p_o = accuracy(gold, pred)
    gc, pc = Counter(gold), Counter(pred)
    p_e = sum((gc[label] / n) * (pc[label] / n) for label in set(gc) | set(pc))
    if abs(1.0 - p_e) < 1e-12:
        return 1.0 if p_o >= 1.0 - 1e-12 else 0.0
    return (p_o - p_e) / (1.0 - p_e)


def confusion_matrix(gold, pred, labels):
    """{gold_label: {pred_label: count}} over the given label order.

    Labels outside ``labels`` are counted under "<other>" so nothing is lost.
    """
    labels = list(labels)
    cols = labels + ["<other>"]
    matrix = {g: {p: 0 for p in cols} for g in cols}
    for g, p in zip(gold, pred):
        g = g if g in labels else "<other>"
        p = p if p in labels else "<other>"
        matrix[g][p] += 1
    # drop the <other> row/col when unused
    if not any(matrix["<other>"].values()) and not any(row["<other>"] for row in matrix.values()):
        del matrix["<other>"]
        for row in matrix.values():
            del row["<other>"]
    return matrix


def format_confusion(matrix):
    """Text table: rows = gold, columns = model."""
    rows = list(matrix)
    cols = list(next(iter(matrix.values()))) if matrix else []
    width = max([len("gold \\ model")] + [len(x) for x in rows + cols]) + 2
    out = ["gold \\ model".ljust(width) + "".join(c.rjust(width) for c in cols)]
    for r in rows:
        out.append(r.ljust(width) + "".join(str(matrix[r][c]).rjust(width) for c in cols))
    return "\n".join(out)


def single_label_report(gold, pred, labels):
    return {
        "n": len(gold),
        "accuracy": accuracy(gold, pred),
        "kappa": cohen_kappa(gold, pred),
        "confusion": confusion_matrix(gold, pred, labels),
    }


# --- multi label --------------------------------------------------------------
def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def multi_label_report(gold_sets, pred_sets, labels):
    """gold_sets / pred_sets: parallel lists of label collections."""
    if len(gold_sets) != len(pred_sets):
        raise ValueError("gold and pred differ in length")
    gold_sets = [set(s) for s in gold_sets]
    pred_sets = [set(s) for s in pred_sets]
    per_label = {}
    tp_all = fp_all = fn_all = 0
    for label in labels:
        tp = sum(1 for g, p in zip(gold_sets, pred_sets) if label in g and label in p)
        fp = sum(1 for g, p in zip(gold_sets, pred_sets) if label not in g and label in p)
        fn = sum(1 for g, p in zip(gold_sets, pred_sets) if label in g and label not in p)
        p, r, f = _prf(tp, fp, fn)
        per_label[label] = {"gold": tp + fn, "pred": tp + fp, "tp": tp,
                            "precision": p, "recall": r, "f1": f}
        tp_all, fp_all, fn_all = tp_all + tp, fp_all + fp, fn_all + fn
    micro_p, micro_r, micro_f = _prf(tp_all, fp_all, fn_all)
    present = [v["f1"] for v in per_label.values() if v["gold"]]
    n = len(gold_sets)
    return {
        "n": n,
        "per_label": per_label,
        "micro_precision": micro_p,
        "micro_recall": micro_r,
        "micro_f1": micro_f,
        "macro_f1": sum(present) / len(present) if present else 0.0,
        "mean_jaccard": sum(jaccard(g, p) for g, p in zip(gold_sets, pred_sets)) / n if n else 0.0,
        "exact_match": sum(1 for g, p in zip(gold_sets, pred_sets) if g == p) / n if n else 0.0,
    }


def format_multi_label(report):
    head = "%-20s %5s %5s %4s %6s %6s %6s" % ("label", "gold", "pred", "tp", "P", "R", "F1")
    lines = [head]
    for label, v in report["per_label"].items():
        lines.append("%-20s %5d %5d %4d %6.2f %6.2f %6.2f" % (
            label, v["gold"], v["pred"], v["tp"], v["precision"], v["recall"], v["f1"]))
    lines.append("micro-F1 %.3f (P %.3f R %.3f)  macro-F1 %.3f  mean Jaccard %.3f  exact-set match %.3f" % (
        report["micro_f1"], report["micro_precision"], report["micro_recall"],
        report["macro_f1"], report["mean_jaccard"], report["exact_match"]))
    return "\n".join(lines)
