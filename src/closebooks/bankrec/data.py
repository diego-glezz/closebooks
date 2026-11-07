"""Load one phase of the Valtermia dataset into pandas DataFrames.

Layout expected under DATA_ROOT (default data/challenge):
    masters/  sample/  train/  live/  final/  starter/
Amounts are converted to integer cents (`*_c` columns) to avoid float drift in matching.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

PHASES = ("sample", "train", "live", "final")


def _read_csv(path: Path) -> pd.DataFrame:
    """Read a csv / csv.gz as strings, trying utf-8 then cp1252."""
    raw = path.read_bytes()
    if path.suffix == ".gz":
        import gzip

        raw = gzip.decompress(raw)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


def cents(s: pd.Series) -> pd.Series:
    num = pd.to_numeric(s.replace("", None), errors="coerce").fillna(0.0)
    return (num * 100).round().astype("int64")


def _concat_dir(folder: Path, pattern: str = "*.csv") -> pd.DataFrame:
    files = sorted(folder.glob(pattern)) if folder.exists() else []
    frames = []
    for f in files:
        df = _read_csv(f)
        df["_file"] = f.name
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


@dataclass
class Masters:
    customers: pd.DataFrame
    suppliers: pd.DataFrame
    accounts: pd.DataFrame
    entities: pd.DataFrame
    terms: pd.DataFrame
    fx: pd.DataFrame
    categories: list[str]


@dataclass
class Phase:
    name: str
    root: Path
    masters: Masters
    bank: pd.DataFrame
    items: pd.DataFrame
    dd: pd.DataFrame = field(default_factory=pd.DataFrame)
    cards: pd.DataFrame = field(default_factory=pd.DataFrame)
    cheques: pd.DataFrame = field(default_factory=pd.DataFrame)
    runs: pd.DataFrame = field(default_factory=pd.DataFrame)
    remit_index: pd.DataFrame = field(default_factory=pd.DataFrame)
    pdf_index: pd.DataFrame = field(default_factory=pd.DataFrame)
    golden: pd.DataFrame | None = None
    source: str = ""  # data folder when it differs from `name` (evaluation windows of train)

    @property
    def folder(self) -> Path:
        return self.root / (self.source or self.name)


def load_masters(root: Path, phase: str) -> Masters:
    m = root / "masters"
    final_m = root / "final" / "masters_final"
    if phase == "final" and final_m.exists():
        m = final_m
    with open(m / "categories.csv", encoding="utf-8") as fh:
        cats = [r["category"] for r in csv.DictReader(fh)]
    fx = _read_csv(m / "fx_rates.csv")
    fx["rate"] = pd.to_numeric(fx["units_per_eur"])
    return Masters(
        customers=_read_csv(m / "customers.csv"),
        suppliers=_read_csv(m / "suppliers.csv"),
        accounts=_read_csv(m / "bank_accounts.csv"),
        entities=_read_csv(m / "entities.csv"),
        terms=_read_csv(m / "payment_terms.csv"),
        fx=fx,
        categories=cats,
    )


def _items_from_tables(folder: Path) -> pd.DataFrame:
    parts = []
    p = folder / "ar_invoices.csv.gz"
    if p.exists():
        ar = _read_csv(p)
        parts.append(
            pd.DataFrame(
                {
                    "item_id": ar["item_id"],
                    "kind": "ARI",
                    "side": "AR",
                    "entity": ar["entity"],
                    "number": ar["document_number"],
                    "issue_date": ar["issue_date"],
                    "due_date": ar["due_date"],
                    "party_id": ar["customer_id"],
                    "party_name": ar["customer_name"],
                    "currency": ar["currency"],
                    "total_c": cents(ar["total_amount"]),
                    "terms": ar["payment_terms"],
                    "po": ar["customer_po"],
                    "method": ar["payment_method"],
                    "related": "",
                }
            )
        )
    p = folder / "ap_invoices.csv.gz"
    if p.exists():
        ap = _read_csv(p)
        parts.append(
            pd.DataFrame(
                {
                    "item_id": ap["item_id"],
                    "kind": "API",
                    "side": "AP",
                    "entity": ap["entity"],
                    "number": ap["supplier_invoice_number"],
                    "issue_date": ap["issue_date"],
                    "due_date": ap["due_date"],
                    "party_id": ap["supplier_id"],
                    "party_name": ap["supplier_name"],
                    "currency": ap["currency"],
                    "total_c": cents(ap["total_amount"]),
                    "terms": ap["payment_terms"],
                    "po": ap["our_po"],
                    "method": ap["payment_method"],
                    "related": "",
                }
            )
        )
    p = folder / "credit_notes.csv.gz"
    if p.exists():
        cn = _read_csv(p)
        parts.append(
            pd.DataFrame(
                {
                    "item_id": cn["item_id"],
                    "kind": cn["item_id"].str[:3],
                    "side": cn["side"],
                    "entity": cn["entity"],
                    "number": cn["document_number"],
                    "issue_date": cn["issue_date"],
                    "due_date": cn["issue_date"],
                    "party_id": cn["party_id"],
                    "party_name": cn["party_name"],
                    "currency": cn["currency"],
                    "total_c": cents(cn["total_amount"]),
                    "terms": "",
                    "po": "",
                    "method": "",
                    "related": cn["related_document"],
                }
            )
        )
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def load_items(root: Path, phase: str) -> pd.DataFrame:
    folder = root / phase
    items = _items_from_tables(folder)
    items["open_c"] = items["total_c"]
    start = folder / "open_items_at_start.csv.gz"
    if start.exists():
        st = _read_csv(start)
        # attributes of carried-over items come from earlier phases when available
        prev = (
            pd.concat(
                [
                    _items_from_tables(root / p)
                    for p in ("train", "live")
                    if p != phase and (root / p).exists()
                ],
                ignore_index=True,
            )
            if any((root / p).exists() for p in ("train", "live") if p != phase)
            else pd.DataFrame()
        )
        carried = pd.DataFrame(
            {
                "item_id": st["item_id"],
                "kind": st["item_id"].str[:3],
                "side": st["item_id"].str[:2].map({"AR": "AR", "AP": "AP"}),
                "entity": st["entity"],
                "number": st["document_number"],
                "issue_date": st["issue_date"],
                "due_date": st["due_date"],
                "party_id": st["party_id"],
                "party_name": "",
                "currency": st["currency"],
                "total_c": cents(st["total_amount"]),
                "terms": "",
                "po": "",
                "method": "",
                "related": "",
                "open_c": cents(st["open_amount"]),
            }
        )
        if len(prev):
            attrs = prev.drop_duplicates("item_id").set_index("item_id")
            for col in ("party_name", "terms", "po", "method", "related"):
                carried[col] = carried["item_id"].map(attrs[col]).fillna("")
        items = pd.concat(
            [carried, items[~items["item_id"].isin(carried["item_id"])]], ignore_index=True
        )
    items = items.drop_duplicates("item_id", keep="first").reset_index(drop=True)
    return items


# Detail files (batches, payment runs, advices) of the phases before the one we run: the bank
# books a debit or credit days after its detail file is dated, so the first days of a phase cite
# files that live in the previous phase's folder (evaluation windows already look back).
PRIOR_PHASES = {"sample": ("train",), "live": ("train",), "final": ("train", "live")}
LOOKBACK_DAYS = 120  # batches / payment runs (confirming debits come weeks later)
ADVICE_LOOKBACK_DAYS = 20  # remittance advices (they arrive days after the cash)


def _prior_rows(
    root: Path, phase: str, sub: str, col: str, start: str, back: int, pattern: str = "*.csv"
) -> pd.DataFrame:
    """Rows of `sub/` of the earlier phases dated in [start - back, start) (never the phase itself)."""
    lo = (pd.Timestamp(start) - pd.Timedelta(days=back)).strftime("%Y-%m-%d")
    frames = []
    for prior in PRIOR_PHASES.get(phase, ()):
        df = _concat_dir(root / prior / sub, pattern)
        if df.empty or col not in df:
            continue
        frames.append(df[(df[col] >= lo) & (df[col] < start)])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _with_prior(own: pd.DataFrame, prior: pd.DataFrame) -> pd.DataFrame:
    if prior.empty:
        return own
    if own.empty:
        return prior.reset_index(drop=True)
    both = pd.concat([own, prior], ignore_index=True)
    return both.drop_duplicates(subset=[c for c in both.columns if c != "_file"]).reset_index(
        drop=True
    )


def _prior_advice_index(root: Path, phase: str, start: str) -> pd.DataFrame:
    lo = (pd.Timestamp(start) - pd.Timedelta(days=ADVICE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    frames = []
    for prior in PRIOR_PHASES.get(phase, ()):
        d = root / prior / "remittance_advices"
        if not (d / "index.csv").exists():
            continue
        df = _read_csv(d / "index.csv")
        df = df[(df["received_date"] >= lo) & (df["received_date"] < start)].copy()
        df["_dir"] = d.as_posix()  # the advice file is under its own phase's folder
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_phase(root: Path | str, phase: str) -> Phase:
    root = Path(root)
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    folder = root / phase
    bank = _read_csv(folder / "bank_transactions.csv.gz")
    bank["amount_c"] = cents(bank["amount"])
    bank["orig_amount_c"] = cents(bank["orig_amount"])
    bank = bank.sort_values(["booking_date", "bank_txn_id"]).reset_index(drop=True)
    start = str(bank["booking_date"].min())
    remit = (
        _read_csv(folder / "remittance_advices" / "index.csv")
        if (folder / "remittance_advices" / "index.csv").exists()
        else pd.DataFrame()
    )
    prior_remit = _prior_advice_index(root, phase, start)
    if not prior_remit.empty:
        remit = pd.concat([remit, prior_remit], ignore_index=True).fillna("")
    ph = Phase(
        name=phase,
        root=root,
        masters=load_masters(root, phase),
        bank=bank,
        items=load_items(root, phase),
        dd=_with_prior(
            _concat_dir(folder / "direct_debit_batches"),
            _prior_rows(root, phase, "direct_debit_batches", "collection_date", start, LOOKBACK_DAYS),
        ),
        cards=_with_prior(
            _concat_dir(folder / "card_settlements"),
            _prior_rows(root, phase, "card_settlements", "settlement_date", start, LOOKBACK_DAYS),
        ),
        cheques=_with_prior(
            _concat_dir(folder / "cheque_deposits"),
            _prior_rows(root, phase, "cheque_deposits", "deposit_date", start, LOOKBACK_DAYS),
        ),
        runs=_with_prior(
            _concat_dir(folder / "payment_runs"),
            _prior_rows(root, phase, "payment_runs", "execution_date", start, LOOKBACK_DAYS),
        ),
        remit_index=remit,
        pdf_index=_read_csv(folder / "pdfs" / "index.csv")
        if (folder / "pdfs" / "index.csv").exists()
        else pd.DataFrame(),
    )
    g = folder / "golden_allocations.csv.gz"
    if g.exists():
        ph.golden = _read_csv(g)
    return ph


# ---------------------------------------------------------------- evaluation windows


def load_window(root: Path | str, start: str, end: str, name: str | None = None) -> Phase:
    """A LIVE-like phase cut out of TRAIN: bank lines booked in [start, end], items open at
    `start` (open amount rebuilt from the golden allocations booked before `start`) plus items
    issued in the window, and the golden of the window. Lets us measure exactly the situation
    of LIVE/FINAL with known answers."""
    import duckdb

    root = Path(root)
    folder = root / "train"
    name = name or f"train_{start[:7]}_{end[:7]}"
    con = duckdb.connect()
    con.execute(
        f"create view b as select * from read_csv_auto('{folder.as_posix()}/"
        f"bank_transactions.csv.gz', all_varchar=true)"
    )
    con.execute(
        f"create view g as select * from read_csv_auto('{folder.as_posix()}/"
        f"golden_allocations.csv.gz', all_varchar=true)"
    )
    bank = con.execute(f"select * from b where booking_date between '{start}' and '{end}'").df()
    bank = bank.fillna("")
    golden = (
        con.execute(
            f"select g.* from g join b using(bank_txn_id) where b.booking_date between '{start}' "
            f"and '{end}'"
        )
        .df()
        .fillna("")
    )
    # cents settled per item before the window (RETURNED re-opens)
    settled = con.execute(f"""
        select g.item_id, sum(case when g.exception = 'RETURNED' then -cast(g.amount as double)
                                 when g.exception = 'DUPLICATE_PAYMENT' then 0
                                 else cast(g.amount as double) end) as paid,
               bool_or(g.exception in ('SHORT_DISCOUNT','SHORT_BANK_CHARGES','SHORT_WITHHOLDING',
                                     'FX_DIFFERENCE','OVERPAYMENT')) as closed
        from g join b using(bank_txn_id)
        where b.booking_date < '{start}' and g.item_id is not null and g.item_id <> ''
        group by 1""").df()
    items = _items_from_tables(folder)
    items = items[items["issue_date"] <= end].copy()
    paid = settled.set_index("item_id")
    paid_c = (items["item_id"].map(paid["paid"]).fillna(0.0) * 100).round().astype("int64")
    closed = items["item_id"].map(paid["closed"]).fillna(False).astype(bool)
    items["open_c"] = (items["total_c"] - paid_c).clip(lower=0)
    items.loc[closed, "open_c"] = 0
    before = items["issue_date"] < start
    items = items[~before | (items["open_c"] > 0)].reset_index(drop=True)

    def window(df: pd.DataFrame, col: str, back: int = 62, fwd: int = 10) -> pd.DataFrame:
        if df.empty or col not in df:
            return df
        lo = (pd.Timestamp(start) - pd.Timedelta(days=back)).strftime("%Y-%m-%d")
        hi = (pd.Timestamp(end) + pd.Timedelta(days=fwd)).strftime("%Y-%m-%d")
        return df[(df[col] >= lo) & (df[col] <= hi)].reset_index(drop=True)

    bank["amount_c"] = cents(bank["amount"])
    bank["orig_amount_c"] = cents(bank["orig_amount"])
    bank = bank.sort_values(["booking_date", "bank_txn_id"]).reset_index(drop=True)
    remit = folder / "remittance_advices" / "index.csv"
    pdfs = folder / "pdfs" / "index.csv"
    pdf_index = _read_csv(pdfs) if pdfs.exists() else pd.DataFrame()
    if not pdf_index.empty:  # documents of items we can still see (open or in the window)
        lo = (pd.Timestamp(start) - pd.Timedelta(days=550)).strftime("%Y-%m-%d")
        pdf_index = pdf_index[
            (pdf_index["document_date"] >= lo) & (pdf_index["document_date"] <= end)
        ].reset_index(drop=True)
    return Phase(
        name=name,
        source="train",
        root=root,
        masters=load_masters(root, "train"),
        bank=bank,
        items=items,
        dd=window(_concat_dir(folder / "direct_debit_batches"), "collection_date"),
        cards=window(_concat_dir(folder / "card_settlements"), "settlement_date"),
        cheques=window(_concat_dir(folder / "cheque_deposits"), "deposit_date"),
        runs=window(_concat_dir(folder / "payment_runs"), "execution_date", back=120),
        remit_index=window(_read_csv(remit), "received_date", back=40, fwd=40)
        if remit.exists()
        else pd.DataFrame(),
        pdf_index=pdf_index,
        golden=golden,
    )
