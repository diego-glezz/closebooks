"""Text normalization for bank concepts, document numbers and party names.

Bank texts are noisy (POLICIES §5.5): numbers without prefix or leading zeros, truncated at
16/35/38 chars, typos, names truncated or trade names. Everything here is pure and fast.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9/\-\.]*[A-Z0-9]|[A-Z0-9]")
_DIGITS = re.compile(r"\d+")
_AMOUNT = re.compile(r"(?<![\w/-])(?:\d{1,3}(?:[.,]\d{3})+|\d+)[.,]\d{2}(?![\w/-])")

LEGAL_SUFFIXES = {
    "SL",
    "SLU",
    "SA",
    "SAU",
    "SAS",
    "SARL",
    "EURL",
    "SCOOP",
    "COOP",
    "GMBH",
    "CO",
    "KG",
    "EK",
    "AG",
    "LTD",
    "LIMITED",
    "LLC",
    "INC",
    "CORP",
    "PLC",
    "BV",
    "NV",
    "SRL",
    "LDA",
    "SP",
    "ZOO",
    "SA DE CV",
    "DE",
    "CV",
    "LTDA",
    "SARLAU",
    "PTY",
    "THE",
    "Y",
    "AND",
    "ET",
    "UND",
    "E",
}


@lru_cache(maxsize=200_000)
def fold(text: str | None) -> str:
    """Uppercase, strip accents. Keeps punctuation."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return t.upper()


def alnum(text: str | None) -> str:
    return _NON_ALNUM.sub("", fold(text))


def words(text: str | None) -> list[str]:
    return [w for w in _NON_ALNUM.split(fold(text)) if w]


def name_key(name: str | None) -> str:
    """'Aguas Calvo Plus S.L.U.' -> 'AGUAS CALVO PLUS' (legal suffixes and punctuation removed)."""
    ws = [w for w in words(name) if w not in LEGAL_SUFFIXES]
    return " ".join(ws)


# ---------------------------------------------------------------- document numbers


def number_keys(number: str | None) -> set[str]:
    """Index keys for a printed document number, so noisy bank references can find it.

    'FV25-0211247' -> {'FV250211247', '250211247', '0211247', '211247'}
    'F25/01964'    -> {'F2501964', '2501964', '01964', '1964'?no: min 5 chars}
    'INV0001312'   -> {'INV0001312', '0001312', '1312'?no}
    """
    if not number:
        return set()
    full = alnum(number)
    keys = {full} if len(full) >= 4 else set()
    digits = "".join(_DIGITS.findall(full))
    if len(digits) >= 5:
        keys.add(digits)
        keys.add(digits.lstrip("0") or "0")
    # last digit group (after the year/prefix part), with and without leading zeros
    groups = _DIGITS.findall(fold(number))
    if groups:
        last = groups[-1]
        if len(last) >= 5:
            keys.add(last)
            if len(last.lstrip("0")) >= 5:
                keys.add(last.lstrip("0"))
    # cited without prefix/year: 'RE250108497' quoted as '108497' or '0108497'
    if len(digits) >= 8:
        for n in (6, 7):
            tail = digits[-n:]
            if len(tail.lstrip("0")) >= 5:
                keys.add(tail)
                keys.add(tail.lstrip("0"))
    return {k for k in keys if len(k) >= 5}


def text_tokens(text: str | None) -> set[str]:
    """Candidate reference tokens found in a bank text (normalized like number_keys)."""
    if not text:
        return set()
    t = _AMOUNT.sub(" ", fold(text))
    out: set[str] = set()
    for tok in _TOKEN.findall(t):
        a = _NON_ALNUM.sub("", tok)
        if len(a) >= 5:
            out.add(a)
            digits = "".join(_DIGITS.findall(a))
            if len(digits) >= 5:
                out.add(digits)
                out.add(digits.lstrip("0") or "0")
    for d in _DIGITS.findall(t):
        if len(d) >= 5:
            out.add(d)
            out.add(d.lstrip("0") or "0")
    return {k for k in out if len(k) >= 5}


MONTHS = {
    "ENE": 1,
    "ENERO": 1,
    "JAN": 1,
    "JANUARY": 1,
    "JANV": 1,
    "JANVIER": 1,
    "JANUAR": 1,
    "FEB": 2,
    "FEBRERO": 2,
    "FEBRUARY": 2,
    "FEV": 2,
    "FEVRIER": 2,
    "FEBRUAR": 2,
    "MAR": 3,
    "MARZO": 3,
    "MARCH": 3,
    "MARS": 3,
    "MARZ": 3,
    "ABR": 4,
    "ABRIL": 4,
    "APR": 4,
    "APRIL": 4,
    "AVR": 4,
    "AVRIL": 4,
    "MAY": 5,
    "MAYO": 5,
    "MAI": 5,
    "JUN": 6,
    "JUNIO": 6,
    "JUNE": 6,
    "JUIN": 6,
    "JUNI": 6,
    "JUL": 7,
    "JULIO": 7,
    "JULY": 7,
    "JUIL": 7,
    "JUILLET": 7,
    "JULI": 7,
    "AGO": 8,
    "AGOSTO": 8,
    "AUG": 8,
    "AUGUST": 8,
    "AOUT": 8,
    "SEP": 9,
    "SEPT": 9,
    "SEPTIEMBRE": 9,
    "SEPTEMBER": 9,
    "SEPTEMBRE": 9,
    "OCT": 10,
    "OCTUBRE": 10,
    "OCTOBER": 10,
    "OCTOBRE": 10,
    "OKT": 10,
    "OKTOBER": 10,
    "NOV": 11,
    "NOVIEMBRE": 11,
    "NOVEMBER": 11,
    "NOVEMBRE": 11,
    "DIC": 12,
    "DICIEMBRE": 12,
    "DEC": 12,
    "DECEMBER": 12,
    "DECEMBRE": 12,
    "DEZ": 12,
    "DEZEMBER": 12,
}
_SERVICE_HINT = re.compile(
    r"\b(MANT|MANTENIMIENTO|SERVICIOS?|SERVICE|CUOTA|MAINTENANCE|ABONO MENSUAL)\b"
)


def service_month(text: str | None) -> int | None:
    """'MANT. MAY 25' / 'SERVICIOS MAYO 2025' -> 5. None if no month named."""
    t = fold(text)
    if not _SERVICE_HINT.search(t):
        return None
    for w in words(t):
        if w in MONTHS:
            return MONTHS[w]
    return None
