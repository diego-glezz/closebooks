"""Parse remittance advices (csv/txt/xlsx/pdf) and free-text clauses of document PDFs.

PDF text extraction is slow (~10 ms/page): results are cached in out/cache/pdf_text_<phase>.json.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from closebooks.bankrec.text import alnum, fold

_AMT = r"-?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{2})|-?\d+(?:[.,]\d{2})"
_DATE = r"\d{2}/\d{2}/\d{2,4}"


def parse_amount(s: str) -> int:
    """'11.302,46' / '1,295.52' / '3 735,43' / '-21,16' -> cents."""
    s = s.strip().replace(" ", "").replace(" ", "")
    neg = s.startswith("-")
    s = s.lstrip("-+")
    if "," in s and "." in s:
        s = (
            s.replace(".", "").replace(",", ".")
            if s.rfind(",") > s.rfind(".")
            else s.replace(",", "")
        )
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) == 2 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[-1]) == 3):
        s = s.replace(".", "")
    v = round(float(s) * 100)
    return -v if neg else v


@dataclass
class AdviceLine:
    number: str
    gross_c: int
    deduction_c: int = 0
    reason: str = ""
    paid_c: int | None = None

    @property
    def paid(self) -> int:
        return self.paid_c if self.paid_c is not None else self.gross_c + self.deduction_c


@dataclass
class Advice:
    file: str
    entity: str
    payer: str
    received: str
    ref: str = ""
    pay_date: str = ""
    currency: str = ""
    total_c: int | None = None
    lines: list[AdviceLine] = field(default_factory=list)

    @property
    def paid_total(self) -> int:
        return sum(ln.paid for ln in self.lines)


_LINE_RE = re.compile(
    rf"^\s*([A-Z]{{0,4}}[\w/\-\.]*\d[\w/\-\.]*)\s+({_DATE})\s+({_AMT})(?:\s+({_AMT}))?\s*(.*)$"
)
_REF_RE = re.compile(r"^Subject:.*?([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)\s*$", re.I | re.M)
_TOTAL_RE = re.compile(rf"({_AMT})\s*(EUR|USD|GBP|CHF|PLN|MXN|BRL|MAD|CNY|AUD|CAD|ZAR)")


def _parse_txt(text: str, adv: Advice) -> None:
    m = _REF_RE.search(text)
    if m:
        adv.ref = m.group(1)
    m = _TOTAL_RE.search(text)
    if m:
        adv.total_c, adv.currency = parse_amount(m.group(1)), m.group(2)
    for line in text.splitlines():
        lm = _LINE_RE.match(line)
        if not lm:
            continue
        number, _, gross, ded, rest = lm.groups()
        ln = AdviceLine(number=number, gross_c=parse_amount(gross))
        if ded:
            ln.deduction_c = parse_amount(ded)
            ln.reason = rest.strip()
        adv.lines.append(ln)


_NUM_HEADERS = ("document", "invoice", "doc", "factura", "rechnung", "facture", "documento")


def _number_column(cells: list[str]) -> int | None:
    """Column that holds OUR document number. It is the first column in the usual layouts, but
    the layout a key account switched to in September 2025 starts with its own voucher number
    ("Voucher, Invoice, PO, Inv. date, Amount, Deduction, Net paid"): our number, without
    prefix, is in the "Invoice" column."""
    low = [c.lower() for c in cells]
    for name in _NUM_HEADERS:
        if name in low:
            return low.index(name)
    return None


def _parse_csv_rows(rows: list[list[str]], adv: Advice) -> None:
    rows = [[c.strip() for c in r] for r in rows if r and any(c.strip() for c in r)]
    header_at = next((k for k, r in enumerate(rows) if _number_column(r) is not None), None)
    if header_at is None:
        return
    num_col = _number_column(rows[header_at]) or 0
    for r in rows[
        :header_at
    ]:  # metadata rows: "payment_ref;X;payment_date;Y" or "Payment advice;X;date"
        low = [c.lower() for c in r]
        if "payment_ref" in low:
            kv = dict(zip(low[0::2], r[1::2], strict=False))
            adv.ref = kv.get("payment_ref", adv.ref)
            adv.pay_date = kv.get("payment_date", adv.pay_date)
            adv.currency = kv.get("currency", adv.currency)
        elif low[0] in ("payment advice", "aviso de pago", "avis de paiement", "zahlungsavis"):
            adv.ref = r[1] if len(r) > 1 else adv.ref
            adv.pay_date = r[2] if len(r) > 2 else adv.pay_date
    header = [h.lower() for h in rows[header_at]]
    idx = {h: n for n, h in enumerate(header)}
    for r in rows[header_at + 1 :]:
        if r[0].upper() in ("TOTAL", "TOTALES", "SUMME", "TOTAL GENERAL") or num_col >= len(r):
            continue

        def get(*names: str, r: list[str] = r) -> str:
            for c in names:
                if c in idx and idx[c] < len(r) and r[idx[c]] not in ("", "None"):
                    return r[idx[c]]
            return ""

        try:
            ln = AdviceLine(
                number=r[num_col], gross_c=parse_amount(get("gross", "importe", "amount") or "0")
            )
            ded = get("discount_or_deduction", "deduction", "deduccion", "abzug")
            if ded and parse_amount(ded) != 0:
                ln.deduction_c = -abs(parse_amount(ded))
            ln.reason = get("reason", "motivo", "grund")
            if get("paid", "pagado", "bezahlt", "net paid"):
                ln.paid_c = parse_amount(get("paid", "pagado", "bezahlt", "net paid"))
        except ValueError:
            continue
        adv.lines.append(ln)


def _parse_pdf_text(text: str, adv: Advice) -> None:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low.startswith(("payment ref", "ref", "referencia", "zahlungsref")) and i + 1 < len(
            lines
        ):
            adv.ref = adv.ref or lines[i + 1]
        if low in ("amount", "importe", "betrag", "montant") and i + 1 < len(lines):
            m = _TOTAL_RE.search(lines[i + 1])
            if m:
                adv.total_c, adv.currency = parse_amount(m.group(1)), m.group(2)
    # table: number, date, gross, [deduction], paid  (one value per line)
    i = 0
    while i < len(lines):
        if (
            re.match(r"^[A-Z]{0,4}[\w/\-]*\d{4,}[\w/\-]*$", lines[i])
            and i + 1 < len(lines)
            and re.match(_DATE, lines[i + 1])
        ):
            vals = []
            j = i + 2
            while j < len(lines) and re.fullmatch(_AMT, lines[j]):
                vals.append(parse_amount(lines[j]))
                j += 1
            reason = ""
            if (
                j < len(lines)
                and not re.match(r"^[A-Z]{0,4}[\w/\-]*\d{4,}", lines[j])
                and not lines[j].startswith("Documento")
            ):
                reason = lines[j]
            if vals:
                ln = AdviceLine(number=lines[i], gross_c=vals[0], paid_c=vals[-1])
                if len(vals) >= 3:
                    ln.deduction_c = -abs(vals[1])
                    ln.reason = reason
                adv.lines.append(ln)
            i = j
        else:
            i += 1


def load_advices(folder: Path, index_rows: list[dict]) -> list[Advice]:
    import openpyxl
    import pypdf

    out: list[Advice] = []
    for r in index_rows:
        path = Path(r["_folder"]) / r["file"] if r.get("_folder") else folder / r["file"]
        if not path.exists():
            continue
        adv = Advice(
            file=r["file"],
            entity=r.get("entity", ""),
            payer=r.get("payer_name", ""),
            received=r.get("received_date", ""),
        )
        fmt = r.get("format") or path.suffix.lstrip(".")
        try:
            if fmt == "csv":
                text = path.read_bytes().decode("utf-8-sig", errors="replace")
                first = text.splitlines()[0] if text else ""
                delim = ";" if first.count(";") > first.count(",") else ","
                _parse_csv_rows(list(csv.reader(io.StringIO(text), delimiter=delim)), adv)
            elif fmt == "xlsx":
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                rows = [
                    ["" if v is None else str(v) for v in row]
                    for row in wb.active.iter_rows(values_only=True)
                ]
                _parse_csv_rows(rows, adv)
            elif fmt == "pdf":
                text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(path).pages)
                _parse_pdf_text(text, adv)
            else:
                _parse_txt(path.read_bytes().decode("utf-8", errors="replace"), adv)
        except Exception as exc:  # one bad advice must not stop the run
            adv.ref = adv.ref or f"PARSE_ERROR:{type(exc).__name__}"
        out.append(adv)
    return out


# ---------------------------------------------------------------- document PDFs


def pdf_texts(folder: Path, index_rows: list[dict], cache: Path) -> dict[str, str]:
    import pypdf

    texts: dict[str, str] = {}
    if cache.exists():
        texts = json.loads(cache.read_text(encoding="utf-8"))
    missing = [r for r in index_rows if r["file"] not in texts]
    # rows of an earlier phase (LIVE/FINAL context): reuse the text already extracted for that
    # phase's own runs (pdf_text_train_*.json...) instead of parsing thousands of PDFs again
    for src in sorted({r["_src"] for r in missing if r.get("_src")}):
        want = {r["file"] for r in missing if r.get("_src") == src}
        for other in sorted(cache.parent.glob(f"pdf_text_{src}*.json")):
            if not want:
                break
            seen = json.loads(other.read_text(encoding="utf-8"))
            for f in sorted(want & seen.keys()):
                texts[f] = seen[f]
            want -= seen.keys()
    for r in missing:
        f = r["file"]
        if f in texts:
            continue
        base = Path(r["_folder"]) if r.get("_folder") else folder
        try:
            texts[f] = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(base / f).pages)
        except Exception:
            texts[f] = ""
    if missing:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    return texts


@dataclass
class DocClauses:
    replaces: str = ""  # number of the invoice this one cancels and replaces
    has_instalments: bool = False
    retention: bool = False
    advance: bool = False
    service_month: int | None = None
    service_year: int | None = None
    assigned_to: str = ""  # factoring / confirming entity
    cn_refund: bool = False  # credit note refunded by transfer (not offset)
    new_iban: str = ""
    instalments: list[int] = field(default_factory=list)  # planned amounts (cents)
    payable_c: int | None = None  # amount payable at due date (retention / advance deducted)


_REPL = re.compile(
    r"(?:ANULA Y SUSTITUYE A LA FACTURA|CANCELS AND REPLACES INVOICE|ANNULE ET "
    r"REMPLACE LA FACTURE|ERSETZT DIE RECHNUNG)\s+([A-Z0-9/\-\.]+)"
)
_ASSIGN = re.compile(r"(?:CEDIDO A|ASSIGNED TO|CEDE A|ABGETRETEN AN)\s+(.+?)(?:\.|$)")
_PLAN = re.compile(
    r"\d+\s+(?:vencimientos|instal?lments|[ée]ch[ée]ances|Raten)\b[^()]*\(([^)]*)\)", re.I
)
_PAYABLE = re.compile(
    r"(?:Importe (?:neto )?a pagar(?: al vencimiento)?|Amount payable at due date|"
    r"Net amount payable|Montant (?:net )?[àa] payer|Zahlbetrag|Zu zahlen)"
    r"\s*:?\s*(-?\d[\d .,]*\d)",
    re.I,
)
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")
_MONTH_YEAR = re.compile(r"\b([A-Z]+)\s+(?:DE\s+)?(20\d{2})\b")


def parse_clauses(text: str) -> DocClauses:
    from closebooks.bankrec.text import MONTHS

    t = fold(text).replace("\n", " ")
    c = DocClauses()
    m = _REPL.search(t)
    if m:
        c.replaces = m.group(1).rstrip(".")
    c.has_instalments = bool(re.search(r"VENCIMIENTOS|PLAZOS|INSTAL?LMENTS|ECHEANCES|RATEN", t))
    c.retention = bool(
        re.search(
            r"RETENCION EN GARANTIA|RETENTION \d|RETENUE DE GARANTIE|"
            r"SICHERHEITSEINBEHALT",
            t,
        )
    )
    c.advance = bool(re.search(r"A DEDUCIR ANTICIPO|ADVANCE RECEIVED|ACOMPTE|ANZAHLUNG", t))
    if re.search(r"MANTENIMIENTO|MAINTENANCE|SERVICE FEE|WARTUNG|ENTRETIEN", t):
        for mm in _MONTH_YEAR.finditer(t):
            if mm.group(1) in MONTHS:
                c.service_month, c.service_year = MONTHS[mm.group(1)], int(mm.group(2))
                break
    flat = " ".join(text.split())
    m = _PLAN.search(flat)
    if m:
        for part in m.group(1).split("/"):
            amt = re.search(_AMT, part.replace(" ", " "))
            if amt:
                with contextlib.suppress(ValueError):
                    c.instalments.append(abs(parse_amount(amt.group(0))))
    m = _PAYABLE.search(flat)
    if m:
        with contextlib.suppress(ValueError):
            c.payable_c = abs(parse_amount(m.group(1)))
    m = _ASSIGN.search(t)
    if m:
        c.assigned_to = m.group(1).strip()
    c.cn_refund = bool(re.search(r"REEMBOLSADO|REFUNDED BY BANK TRANSFER|REMBOURSE|ERSTATTET", t))
    if (
        "CAMBIO DE CUENTA" in t
        or "CHANGE OF BANK" in t
        or "NOUVEAU COMPTE" in t
        or "NEUE BANKVERBINDUNG" in t
        or "NUEVA CUENTA" in t
    ):
        ibans = _IBAN.findall(alnum_keep_spaces(t))
        if ibans:
            c.new_iban = ibans[0]
    return c


def alnum_keep_spaces(t: str) -> str:
    return re.sub(r"[^A-Z0-9 ]", " ", t)


__all__ = [
    "Advice",
    "AdviceLine",
    "DocClauses",
    "alnum",
    "load_advices",
    "parse_amount",
    "parse_clauses",
    "pdf_texts",
]
