"""Write the submission CSV and score it with the OFFICIAL scorer (starter/bankrec_score.py)."""

from __future__ import annotations

import csv
import gzip
import importlib.util
import io
import json
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # output only needs objects with txn / allocs / category / exception / rule
    from closebooks.bankrec.engine import Result

HEADER = ["bank_txn_id", "item_id", "amount", "category", "exception"]
EXCEPTIONS = [
    "PARTIAL",
    "OVERPAYMENT",
    "SHORT_DISCOUNT",
    "SHORT_BANK_CHARGES",
    "SHORT_WITHHOLDING",
    "SHORT_DISPUTE",
    "DUPLICATE_PAYMENT",
    "UNIDENTIFIED",
    "RETURNED",
    "FX_DIFFERENCE",
]


def to_rows(results: list[Result]) -> list[list[str]]:
    rows: list[list[str]] = []
    for r in results:
        seen: set[str] = set()
        allocs = [
            a for a in r.allocs if a.amount_c >= 0 and not (a.item in seen or seen.add(a.item))
        ]
        if allocs:
            rows += [[r.txn, a.item, f"{a.amount_c / 100:.2f}", "", a.exception] for a in allocs]
        else:
            rows.append([r.txn, "", "", r.category or "UNIDENTIFIED", r.exception])
    return rows


def write_csv(results: list[Result], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(to_rows(results))
    return path


def write_explain(results: list[Result], path: Path) -> Path:
    """Our own debug file: rule that produced each line (not for submission)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["bank_txn_id", "rule", "n_items", "category", "exception"])
        for r in results:
            w.writerow(
                [
                    r.txn,
                    r.rule,
                    len(r.allocs),
                    r.category,
                    r.exception or ",".join(sorted({a.exception for a in r.allocs if a.exception})),
                ]
            )
    return path


def rule_counts(results: list[Result]) -> Counter:
    return Counter(r.rule for r in results)


def _scorer(root: Path):
    spec = importlib.util.spec_from_file_location(
        "bankrec_score", root / "starter" / "bankrec_score.py"
    )
    if spec is None or spec.loader is None:
        raise FileNotFoundError("starter/bankrec_score.py not found under the data root")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def score(root: Path, phase: str, submission: Path) -> dict:
    """Score against the phase golden (sample / train) with the official scorer."""
    S = _scorer(root)
    folder = root / phase
    with open(root / "masters" / "categories.csv", encoding="utf-8") as fh:
        cats = [r["category"] for r in csv.DictReader(fh)]
    buf = io.BytesIO()
    with (
        gzip.open(folder / "bank_transactions.csv.gz", "rt", encoding="utf-8") as fh,
        gzip.open(buf, "wt") as out,
    ):
        out.write("bank_txn_id\n")
        for r in csv.DictReader(fh):
            out.write(r["bank_txn_id"] + "\n")
    tmp = submission.parent / f".txns_{phase}.csv.gz"
    tmp.write_bytes(buf.getvalue())
    try:
        g = S.load_golden(str(folder / "golden_allocations.csv.gz"), str(tmp), EXCEPTIONS, cats)
    finally:
        tmp.unlink(missing_ok=True)
    return S.score_submission(g, submission.read_bytes(), submission.name)


def validate(root: Path, phase: str, submission: Path) -> dict:
    """Validate format against the phase bank lines WITHOUT a golden (LIVE / FINAL).

    Runs the official scorer with an empty golden, so format errors match the server's."""
    S = _scorer(root)
    folder = root / phase
    with open(root / "masters" / "categories.csv", encoding="utf-8") as fh:
        cats = [r["category"] for r in csv.DictReader(fh)]
    tmp_g = submission.parent / ".empty_golden.csv.gz"
    with gzip.open(tmp_g, "wt") as out:
        out.write("bank_txn_id,item_id,amount,category,exception\n")
    try:
        g = S.load_golden(str(tmp_g), str(folder / "bank_transactions.csv.gz"), EXCEPTIONS, cats)
    finally:
        tmp_g.unlink(missing_ok=True)
    res = S.score_submission(g, submission.read_bytes(), submission.name)
    return {
        "ok": res["ok"],
        "errors": res["errors"],
        "rows": res["metrics"].get("rows"),
        "txns_covered": res["metrics"].get("txns_covered"),
        "txns_total": res["metrics"].get("txns_total"),
    }


def dump(obj: dict, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def score_phase(ph, submission: Path) -> dict:
    """Score against the golden held in a Phase (works for sample, train and train windows)."""
    import pandas as pd

    S = _scorer(ph.root)
    with open(ph.root / "masters" / "categories.csv", encoding="utf-8") as fh:
        cats = [r["category"] for r in csv.DictReader(fh)]
    tmp_t = submission.parent / ".txns.csv.gz"
    tmp_g = submission.parent / ".golden.csv.gz"
    pd.DataFrame({"bank_txn_id": ph.bank["bank_txn_id"]}).to_csv(tmp_t, index=False)
    ph.golden.to_csv(tmp_g, index=False)
    try:
        g = S.load_golden(str(tmp_g), str(tmp_t), EXCEPTIONS, cats)
    finally:
        tmp_t.unlink(missing_ok=True)
        tmp_g.unlink(missing_ok=True)
    return S.score_submission(g, submission.read_bytes(), submission.name)
