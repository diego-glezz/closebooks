"""Clause / advice caches give the same result as parsing; report-live compares two final.json."""

from __future__ import annotations

from pathlib import Path

from closebooks.bankrec import docs
from closebooks.bankrec.docs import load_advices, parse_clauses, pdf_clauses
from closebooks.cli import compare_finals

ADV_TEXT = (
    "Less advance received against proforma PF25-0100004: 350.02 AUD. Net amount due 816.72 AUD.\n"
)


def test_net_amount_due_is_payable():
    c = parse_clauses(ADV_TEXT)
    assert c.advance and c.payable_c == 81672


def test_pdf_clauses_cache_roundtrip(tmp_path: Path, monkeypatch):
    rows = [{"file": "a.pdf"}, {"file": "b.pdf"}]
    calls = []

    def fake_texts(folder, index_rows, cache):
        calls.append(len(index_rows))
        return {"a.pdf": ADV_TEXT, "b.pdf": ""}

    monkeypatch.setattr(docs, "pdf_texts", fake_texts)
    first = pdf_clauses(tmp_path, rows, tmp_path, "t")
    second = pdf_clauses(tmp_path, rows, tmp_path, "t")
    assert calls == [2]  # second run never opens the texts
    assert first == second and second["a.pdf"].payable_c == 81672


def test_advice_cache_same_as_parse(tmp_path: Path):
    (tmp_path / "x.txt").write_text("Subject: Payment REF-123\n", encoding="utf-8")
    rows = [{"file": "x.txt", "format": "txt", "entity": "ES01"}, {"file": "missing.txt"}]
    cache = tmp_path / "adv.pkl"
    a = load_advices(tmp_path, rows, cache)
    b = load_advices(tmp_path, rows, cache)
    assert a == b == load_advices(tmp_path, rows) and len(a) == 1 and cache.exists()


def test_compare_finals_orders_by_impact():
    a = {
        "score": 87.0,
        "metrics": {
            "txn_accuracy": 0.9,
            "segments": [
                {"segment": "s1", "n": 100, "accuracy": 0.5},
                {"segment": "s2", "n": 1000, "accuracy": 0.9},
            ],
        },
    }
    b = {
        "score": 87.5,
        "metrics": {
            "txn_accuracy": 0.91,
            "segments": [
                {"segment": "s1", "n": 100, "accuracy": 0.7},
                {"segment": "s2", "n": 1000, "accuracy": 0.85},
                {"segment": "s3", "n": 5, "accuracy": 1.0},
            ],
        },
    }
    heads, rows = compare_finals(a, b)
    assert heads[0] == ("score", 87.0, 87.5, 0.5)
    assert [r["segment"] for r in rows][:2] == ["s2", "s1"]
    assert round(rows[0]["lines"]) == -50 and round(rows[1]["lines"]) == 20
    assert rows[2]["a"] is None
