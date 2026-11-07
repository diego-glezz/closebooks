"""LIVE/FINAL see the same earlier context a TRAIN evaluation window sees (made-up files)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from closebooks.bankrec.data import Phase, _prior_phases, _with_prior_context
from closebooks.bankrec.docs import pdf_texts


def _csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def _phase(root: Path, name: str, start: str) -> Phase:
    bank = pd.DataFrame({"bank_txn_id": ["BT-1"], "booking_date": [start]})
    own_pdf = pd.DataFrame(
        [{"file": f"ES01/{name}.pdf", "document_type": "invoice", "document_date": start}]
    )
    return Phase(
        name=name, root=root, masters=None, bank=bank, items=pd.DataFrame(), pdf_index=own_pdf
    )


def test_prior_phases():
    assert _prior_phases("live") == ["train"]
    assert _prior_phases("final") == ["train", "live"]
    assert _prior_phases("train") == [] and _prior_phases("sample") == []


def test_live_gets_train_context_within_the_same_windows_as_dev(tmp_path):
    t = tmp_path / "train"
    _csv(
        t / "pdfs" / "index.csv",
        [
            {"file": "ES01/old.pdf", "document_type": "invoice", "document_date": "2023-12-01"},
            {"file": "ES01/may.pdf", "document_type": "invoice", "document_date": "2025-05-10"},
        ],
    )
    _csv(
        t / "payment_runs" / "ES01_202503_0.csv",
        [
            {"run_id": "R-old", "execution_date": "2025-02-01"},  # > 120 days before
            {"run_id": "R-ok", "execution_date": "2025-06-20"},
        ],
    )
    _csv(
        t / "remittance_advices" / "index.csv",
        [{"file": "ES01/RA_1.txt", "received_date": "2025-06-25"}],
    )
    (tmp_path / "live").mkdir()
    ph = _with_prior_context(_phase(tmp_path, "live", "2025-07-01"), tmp_path, "live")

    files = list(ph.pdf_index["file"])
    assert files == ["ES01/may.pdf", "ES01/live.pdf"]  # older than 550 days dropped
    may = ph.pdf_index.iloc[0]
    assert may["_src"] == "train" and Path(may["_folder"]) == t / "pdfs"
    assert ph.pdf_index.iloc[1]["_folder"] == ""  # own rows keep the phase folder
    assert list(ph.runs["run_id"]) == ["R-ok"]
    assert list(ph.remit_index["file"]) == ["ES01/RA_1.txt"]


def test_train_and_sample_are_untouched(tmp_path):
    _csv(
        tmp_path / "live" / "pdfs" / "index.csv",
        [{"file": "x.pdf", "document_type": "invoice", "document_date": "2025-08-01"}],
    )
    ph = _phase(tmp_path, "sample", "2025-06-02")
    before = ph.pdf_index.copy()
    assert _with_prior_context(ph, tmp_path, "sample").pdf_index.equals(before)


def test_pdf_texts_reuses_the_cache_of_the_source_phase(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "pdf_text_train_2025-01_2025-06.json").write_text(
        json.dumps({"ES01/may.pdf": "pago 2 de 3"}), encoding="utf-8"
    )
    rows = [{"file": "ES01/may.pdf", "_src": "train", "_folder": str(tmp_path / "nowhere")}]
    texts = pdf_texts(tmp_path / "live" / "pdfs", rows, cache_dir / "pdf_text_live.json")
    assert texts["ES01/may.pdf"] == "pago 2 de 3"  # not re-parsed (the folder does not exist)
    saved = json.loads((cache_dir / "pdf_text_live.json").read_text(encoding="utf-8"))
    assert saved["ES01/may.pdf"] == "pago 2 de 3"


def test_earlier_phase_pdf_only_touches_exact_older_items():
    """A TRAIN PDF must not attach clauses (or 'replaces') to a look-alike LIVE number."""
    from types import SimpleNamespace

    from closebooks.bankrec.engine import Engine
    from test_bankrec_rules import make_book

    book = make_book(
        ("ARI-2025-0000001", "F-2025/0100", 10000, "2025-05-10"),  # carried over, exact
        ("ARI-2025-0000002", "F-2025-100", 20000, "2025-08-01"),  # LIVE look-alike (noisy key)
        ("ARI-2025-0000003", "F-2025/0100", 30000, "2025-09-01"),  # same number, issued later
    )
    eng = SimpleNamespace(book=book)
    got = Engine._carried(eng, "F-2025/0100", "AR", {"ES01"}, "2025-05-31")
    assert got == ["ARI-2025-0000001"]
    assert Engine._carried(eng, "F-2025/0100", "AR", {"ES99"}, "2025-05-31") == []
    assert Engine._carried(eng, "F-2025/0100", "AP", {"ES01"}, "2025-05-31") == []
