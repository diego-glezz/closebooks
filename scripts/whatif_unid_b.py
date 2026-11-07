"""What-if on an existing run: put the UNIDENTIFIED exception back on '*_known' lines whose amount
is small (< T, account currency) or round, and re-score with the OFFICIAL scorer.

    python scripts/whatif_unid_b.py out/exp_b/r3_near_unid_known__2024-07-01__2024-12-31 \
        --start 2024-07-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from closebooks.bankrec.data import load_window  # noqa: E402
from closebooks.bankrec.output import score_phase  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--data", default="data/challenge")
    a = ap.parse_args()
    run = Path(a.run)
    ph = load_window(Path(a.data), a.start, a.end)
    sub = pd.read_csv(run / "submission.csv.gz", dtype=str).fillna("")
    expl = pd.read_csv(run / "explain.csv.gz", dtype=str).fillna("")
    known = set(expl.loc[expl["rule"].str.endswith("_known"), "bank_txn_id"])
    amt = dict(zip(ph.bank["bank_txn_id"], ph.bank["amount_c"].astype(int).abs(), strict=True))
    tmp = run / "whatif.csv.gz"
    for t, use_round in ((None, False), (0, True), (1000, False), (1000, True), (2000, True), (3000, True), (5000, True), (10**12, False)):
        s = sub.copy()
        if t is not None:
            def keep(txn: str) -> bool:
                c = amt.get(txn, 0)
                return c < t * 100 or (use_round and c % 100 == 0)

            mask = s["bank_txn_id"].isin(known) & (s["item_id"] == "") & s["bank_txn_id"].map(keep)
            s.loc[mask, "exception"] = "UNIDENTIFIED"
        s.to_csv(tmp, index=False)
        r = score_phase(ph, tmp)
        m = r["metrics"]
        label = "as run (no exception)" if t is None else ("all back = old" if t == 10**12 else f"< {t}" + (" or round" if use_round else ""))
        print(f"{label:24s} score={r['score']:.2f} acc={m['txn_accuracy']:.4f} excF1={m['exception_macro_f1']:.4f}")
    tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
