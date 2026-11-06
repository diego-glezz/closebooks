"""Submission client (network mocked) + score regression on the official sample."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from closebooks import submit as sub

DATA = Path("data/challenge")
HAS_SAMPLE = (DATA / "sample" / "golden_allocations.csv.gz").exists()


class FakeResp:
    def __init__(self, code: int, body: dict | None = None):
        self.status_code, self._body, self.text = code, body or {}, json.dumps(body or {})

    def json(self) -> dict:
        return self._body


def test_submit_flow_never_logs_token(tmp_path, monkeypatch):
    import requests

    f = tmp_path / "s.csv.gz"
    f.write_bytes(b"x")
    monkeypatch.setenv("HK_TOKEN", "secret-token-123")
    monkeypatch.setattr(sub, "LOG_DIR", tmp_path / "log")
    md = tmp_path / "SUBMISSIONS.md"
    md.write_text("| h |\n", encoding="utf-8")
    monkeypatch.setattr(sub, "SUBMISSIONS_MD", md)
    monkeypatch.setattr(sub, "POLL_SECONDS", 0)
    calls = []

    def post(url, **kw):
        calls.append(url)
        if url.endswith("upload-url"):
            assert kw["headers"]["Authorization"] == "Bearer secret-token-123"
            return FakeResp(201, {"sub_id": "S1", "upload": {"url": "https://s3/x", "fields": {}}})
        if url == "https://s3/x":
            return FakeResp(204)
        return FakeResp(200, {"status": "queued"})

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(
        requests, "get", lambda url, **kw: FakeResp(200, {"status": "scored", "score": 61.5})
    )
    res = sub.submit(f, "LIVE", note="test")
    assert res["score"] == 61.5
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["upload-url", "x", "score"]
    log = (tmp_path / "log" / "log.jsonl").read_text(encoding="utf-8")
    assert "secret-token-123" not in log and '"score": 61.5' in log
    assert "61.5" in md.read_text(encoding="utf-8")


def test_preflight_refuses_missing_token_and_bad_phase(tmp_path, monkeypatch):
    monkeypatch.delenv("HK_TOKEN", raising=False)
    with pytest.raises(sub.SubmitError):
        sub._token()
    with pytest.raises(sub.SubmitError):
        sub.preflight(tmp_path / "x.csv", "TRAIN", DATA)


@pytest.mark.skipif(not HAS_SAMPLE, reason="official sample not downloaded")
def test_sample_score_regression(tmp_path):
    """Guardrail: the engine on the official sample must not drop below this score."""
    pytest.importorskip("closebooks.bankrec.engine")
    from closebooks.bankrec.data import load_phase
    from closebooks.bankrec.engine import Engine
    from closebooks.bankrec.output import score, write_csv

    ph = load_phase(DATA, "sample")
    out = write_csv(Engine(ph, cache_dir=Path("out/cache")).run(), tmp_path / "sub.csv.gz")
    res = score(DATA, "sample", out)
    assert res["ok"], res["errors"]
    assert res["score"] >= 84.0, res["metrics"]
