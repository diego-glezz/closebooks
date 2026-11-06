"""Diagnostics of a run against a golden: where the points are lost.

* lost lines by segment (n x (1 - accuracy))
* F1 per exception class (each class weighs the same in the macro-F1)
* allocation-pair precision per engine rule (a rule below ~0.85 usually costs more than it adds)
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _tol(g: float) -> float:
    return max(0.01, 0.005 * abs(g)) + 1e-9


def report(out_dir: Path, golden: pd.DataFrame) -> dict:
    sub = pd.read_csv(out_dir / "submission.csv.gz", dtype=str, keep_default_na=False)
    exp = pd.read_csv(out_dir / "explain.csv.gz", dtype=str, keep_default_na=False)
    rule = exp.set_index("bank_txn_id")["rule"]
    score = json.loads((out_dir / "score.json").read_text(encoding="utf-8"))

    lost = sorted(
        (
            (k, v["n"], v["accuracy"], round(v["n"] * (1 - v["accuracy"])))
            for k, v in score["segments"].items()
        ),
        key=lambda x: -x[3],
    )

    gk = golden.assign(k=golden["bank_txn_id"] + "|" + golden["item_id"]).set_index("k")
    sk = sub.assign(k=sub["bank_txn_id"] + "|" + sub["item_id"]).set_index("k")
    classes = {}
    for c in sorted(set(golden["exception"]) - {""}):
        gold = set(gk.index[gk["exception"] == c])
        ours = set(sk.index[sk["exception"] == c])
        tp = len(gold & ours)
        p = tp / len(ours) if ours else 0.0
        r = tp / len(gold) if gold else 0.0
        classes[c] = {
            "gold": len(gold),
            "ours": len(ours),
            "precision": round(p, 3),
            "recall": round(r, 3),
            "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
        }

    items = sub[sub["item_id"] != ""].copy()
    gold_amt = dict(
        zip(
            gk.index[gk["item_id"] != ""],
            golden.loc[golden["item_id"] != "", "amount"].astype(float),
            strict=True,
        )
    )
    items["rule"] = items["bank_txn_id"].map(rule)
    items["ok"] = [
        k in gold_amt and abs(float(a) - gold_amt[k]) <= _tol(gold_amt[k])
        for k, a in zip(items["bank_txn_id"] + "|" + items["item_id"], items["amount"], strict=True)
    ]
    per_rule = (
        items.groupby("rule")["ok"].agg(["count", "mean"]).sort_values("count", ascending=False)
    )
    rules = {
        k: {
            "pairs": int(v["count"]),
            "precision": round(float(v["mean"]), 3),
            "wrong": round(v["count"] * (1 - v["mean"])),
        }
        for k, v in per_rule.iterrows()
    }
    return {
        "score": score["score"],
        "metrics": score["metrics"],
        "lost_by_segment": lost[:25],
        "exceptions": classes,
        "rules": rules,
    }
