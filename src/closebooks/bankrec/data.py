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


def _index(folder: Path, sub: str) -> pd.DataFrame:
    p = folder / sub / "index.csv"
    return _read_csv(p) if p.exists() else pd.DataFrame()


# How far back load_window() looks for context of a TRAIN window (days before its start).
# LIVE and FINAL get the same context from the phases before them (see _with_prior_context):
# a payment in August can settle an invoice issued in May, whose PDF ("pago 2 de 3",
# retention, replaces...) or payment run was published with the earlier phase.
BATCH_CONTEXT = (  # (Phase attribute, subfolder, date column, days back)
    ("dd", "direct_debit_batches", "collection_date", 62),
    ("cards", "card_settlements", "settlement_date", 62),
    ("cheques", "cheque_deposits", "deposit_date", 62),
    ("runs", "payment_runs", "execution_date", 120),
)
REMIT_DAYS_BACK = 40
PDF_DAYS_BACK = 550
# What LIVE/FINAL take from earlier phases: everything a TRAIN window sees. (A first LIVE try also
# lost ar_dd_batch / ar_card_settlement / ar_1toN_remittance pairs: not because of the batches, but
# because earlier-phase PDFs "replaced" look-alike items of this phase. Engine._carried fixes it.)
PRIOR_CONTEXT = {"dd", "cards", "cheques", "runs", "remit_index", "pdf_index"}


def _prior_phases(phase: str) -> list[str]:
    """Phases whose files can hold context for `phase` (train for live; train + live for final)."""
    if phase not in ("live", "final"):
        return []
    return [p for p in ("train", "live") if PHASES.index(p) < PHASES.index(phase)]


def _with_prior_context(ph: Phase, root: Path, phase: str) -> Phase:
    """Add the documents of earlier phases that a LIVE-like TRAIN window would also see.

    Rows coming from another phase keep their own `file` (no collisions between phases) and get
    `_folder` (where the file lives) and `_src` (the phase), used by docs.pdf_texts/load_advices.
    """
    prior = [p for p in _prior_phases(phase) if (root / p).exists()]
    if not prior or ph.bank.empty:
        return ph
    start = pd.Timestamp(ph.bank["booking_date"].min()[:10])

    def since(days: int) -> str:
        return (start - pd.Timedelta(days=days)).strftime("%Y-%m-%d")

    cut = start.strftime("%Y-%m-%d")
    for attr, sub, col, back in BATCH_CONTEXT:
        if attr not in PRIOR_CONTEXT:
            continue
        extra = []
        for p in prior:
            df = _concat_dir(root / p / sub)
            if not df.empty and col in df:
                extra.append(df[(df[col] >= since(back)) & (df[col] < cut)])
        if extra:
            setattr(ph, attr, pd.concat([*extra, getattr(ph, attr)], ignore_index=True))
    for attr, sub, col, back in (
        ("remit_index", "remittance_advices", "received_date", REMIT_DAYS_BACK),
        ("pdf_index", "pdfs", "document_date", PDF_DAYS_BACK),
    ):
        if attr not in PRIOR_CONTEXT:
            continue
        extra = []
        for p in prior:
            df = _index(root / p, sub)
            if not df.empty and col in df:
                keep = df[col] >= since(back)
                if "document_type" in df:  # bank-details notices never expire (POLICIES §5.11)
                    keep |= df["document_type"] == "bank_account_notice"
                df = df[keep & (df[col] < cut)].copy()
                df["_folder"] = str(root / p / sub)
                df["_src"] = p
                extra.append(df)
        if extra:
            own = getattr(ph, attr)
            setattr(ph, attr, pd.concat([*extra, own], ignore_index=True).fillna(""))
    return ph


def load_phase(root: Path | str, phase: str) -> Phase:
    root = Path(root)
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}")
    folder = root / phase
    bank = _read_csv(folder / "bank_transactions.csv.gz")
    bank["amount_c"] = cents(bank["amount"])
    bank["orig_amount_c"] = cents(bank["orig_amount"])
    bank = bank.sort_values(["booking_date", "bank_txn_id"]).reset_index(drop=True)
    ph = Phase(
        name=phase,
        root=root,
        masters=load_masters(root, phase),
        bank=bank,
        items=load_items(root, phase),
        dd=_concat_dir(folder / "direct_debit_batches"),
        cards=_concat_dir(folder / "card_settlements"),
        cheques=_concat_dir(folder / "cheque_deposits"),
        runs=_concat_dir(folder / "payment_runs"),
        remit_index=_index(folder, "remittance_advices"),
        pdf_index=_index(folder, "pdfs"),
    )
    ph = _with_prior_context(ph, root, phase)
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
        # a new-bank-details notice stays valid however old it is (POLICIES §5.11)
        notice = pdf_index["document_type"] == "bank_account_notice"
        pdf_index = pdf_index[
            ((pdf_index["document_date"] >= lo) | notice) & (pdf_index["document_date"] <= end)
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
