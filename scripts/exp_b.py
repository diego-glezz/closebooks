"""Evaluate one genome of role-B switches on a TRAIN window with the OFFICIAL scorer.

    python scripts/exp_b.py --name ref_fuzzy --flags '{"ref_fuzzy": true}' \
        --start 2024-07-01 --end 2024-12-31

Writes out/exp_b/<name>__<start>__<end>/{submission.csv.gz,explain.csv.gz,result.json}.
The flags are passed through CB_FLAGS (see closebooks.bankrec.variants), set before the import.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path


def parse_flags(spec: str) -> str:
    """'ref_fuzzy=1,subset_max_items=4' (or already JSON) -> JSON string for CB_FLAGS."""
    spec = spec.strip()
    if spec.startswith("{"):
        return spec
    out: dict[str, object] = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        k, _, v = part.partition("=")
        out[k] = int(v) if v.lstrip("-").isdigit() else v.lower() in ("true", "1", "yes", "")
    return json.dumps(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--flags", default="{}")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--data", default="data/challenge")
    ap.add_argument("--out", default="out/exp_b")
    a = ap.parse_args()
    a.flags = parse_flags(a.flags)
    os.environ["CB_FLAGS"] = a.flags
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

    from closebooks.bankrec.data import load_window
    from closebooks.bankrec.engine import Engine
    from closebooks.bankrec.output import score_phase, write_csv, write_explain
    from closebooks.cli import _category_model

    folder = Path(a.out) / f"{a.name}__{a.start}__{a.end}"
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ph = load_window(Path(a.data), a.start, a.end)
    eng = Engine(ph, cache_dir=Path("out/cache"), category_model=_category_model(Path(a.data), ph.name, a.start))
    results = eng.run()
    sub = folder / "submission.csv.gz"
    write_csv(results, sub)
    write_explain(results, folder / "explain.csv.gz")
    res = score_phase(ph, sub)

    # pair precision by rule: the explain file knows the rule, the golden knows the truth
    import pandas as pd

    gold = ph.golden
    pairs = set(
        zip(gold["bank_txn_id"], gold["item_id"].fillna(""), strict=True)
    )
    by_rule: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    sub_df = pd.read_csv(sub, dtype=str).fillna("")
    expl = pd.read_csv(folder / "explain.csv.gz", dtype=str).fillna("")
    rule_of = dict(zip(expl["bank_txn_id"], expl["rule"], strict=True))
    for t, i in zip(sub_df["bank_txn_id"], sub_df["item_id"], strict=True):
        if not i:
            continue
        k = rule_of.get(t, "?")
        by_rule[k][0] += 1
        by_rule[k][1] += (t, i) in pairs
    # monthly scores (same run, official scorer on each month): spread and paired comparisons
    from types import SimpleNamespace

    monthly: dict[str, float] = {}
    months = sorted({d[:7] for d in ph.bank["booking_date"]})
    for mon in months:
        ids = set(ph.bank.loc[ph.bank["booking_date"].str[:7] == mon, "bank_txn_id"])
        part = sub_df[sub_df["bank_txn_id"].isin(ids)]
        msub = folder / f"month_{mon}.csv.gz"
        part.to_csv(msub, index=False)
        sn = SimpleNamespace(
            root=ph.root,
            bank=ph.bank[ph.bank["bank_txn_id"].isin(ids)],
            golden=ph.golden[ph.golden["bank_txn_id"].isin(ids)],
        )
        monthly[mon] = score_phase(sn, msub)["score"]
        msub.unlink(missing_ok=True)
    m = res["metrics"]
    out = {
        "monthly": monthly,
        "name": a.name,
        "flags": json.loads(a.flags),
        "start": a.start,
        "end": a.end,
        "ok": res["ok"],
        "score": res["score"],
        "alloc_f1": m["allocation_f1"],
        "precision": m["precision"],
        "recall": m["recall"],
        "txn_accuracy": m["txn_accuracy"],
        "exc_macro_f1": m["exception_macro_f1"],
        "seconds": round(time.time() - t0, 1),
        "rule_pairs": {k: v for k, v in sorted(by_rule.items())},
        "segments": {k: v for k, v in res["segments"].items()},
    }
    (folder / "result.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"{a.name} {a.start}..{a.end} score={out['score']} P={out['precision']:.4f} "
          f"R={out['recall']:.4f} t={out['seconds']}s")


if __name__ == "__main__":
    main()
