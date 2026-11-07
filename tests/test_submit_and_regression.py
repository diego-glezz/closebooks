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


def _fake_server(monkeypatch, poll_bodies):
    """upload-url (with presigned credentials) -> S3 -> score -> GET returns poll_bodies."""
    import requests

    def post(url, **kw):
        if url.endswith("upload-url"):
            return FakeResp(
                201,
                {
                    "sub_id": "S9",
                    "upload": {
                        "url": "https://s3/x",
                        "fields": {"key": "k", "x-amz-signature": "SIG", "policy": "POL"},
                    },
                },
            )
        if url == "https://s3/x":
            return FakeResp(204)
        return FakeResp(200, {"status": "queued"})

    bodies = iter(poll_bodies)

    def get(url, **kw):
        body = next(bodies)
        if isinstance(body, Exception):
            raise body
        return FakeResp(200, body)

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)


def _setup(tmp_path, monkeypatch):
    run = tmp_path / "out_live"
    run.mkdir()
    f = run / "submission.csv.gz"
    f.write_bytes(b"csv-bytes")
    (run / "explain.csv.gz").write_bytes(b"explain-bytes")
    monkeypatch.setenv("HK_TOKEN", "secret-token-123")
    monkeypatch.setattr(sub, "LOG_DIR", tmp_path / "log")
    md = tmp_path / "SUBMISSIONS.md"
    md.write_text("| h |\n", encoding="utf-8")
    monkeypatch.setattr(sub, "SUBMISSIONS_MD", md)
    monkeypatch.setattr(sub, "POLL_SECONDS", 0)
    return f, md


def test_submit_saves_file_and_every_server_response(tmp_path, monkeypatch):
    f, _ = _setup(tmp_path, monkeypatch)
    final = {"status": "scored", "score": 70.1, "metrics": {"allocation_f1": 0.8}, "segments": [1]}
    _fake_server(monkeypatch, [{"status": "queued"}, {"status": "scoring"}, final])
    sub.submit(f, "LIVE", note="n")

    (folder,) = [p for p in (tmp_path / "log").iterdir() if p.is_dir()]
    assert (folder / "submission.csv.gz").read_bytes() == b"csv-bytes"
    assert (folder / "explain.csv.gz").read_bytes() == b"explain-bytes"
    steps = [
        json.loads(x) for x in (folder / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    # repeated "queued" polls are not stored; every change of status is
    assert [s["step"] for s in steps] == ["upload-url", "s3-upload", "score", "poll", "poll"]
    raw = (folder / "responses.jsonl").read_text(encoding="utf-8")
    assert "SIG" not in raw and "POL" not in raw and "secret-token-123" not in raw
    assert json.loads((folder / "final.json").read_text(encoding="utf-8")) == final
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["sub_id"] == "S9" and meta["status"] == "scored" and meta["score"] == 70.1


def test_network_error_while_polling_keeps_sub_id_and_fetch_recovers(tmp_path, monkeypatch):
    import requests

    f, md = _setup(tmp_path, monkeypatch)
    _fake_server(monkeypatch, [requests.ConnectionError("wifi down")])
    with pytest.raises(sub.SubmitError, match="closebooks fetch S9"):
        sub.submit(f, "LIVE", note="n")
    (entry,) = sub.history()
    assert entry["status"] == "poll-error" and entry["sub_id"] == "S9"

    monkeypatch.setattr(
        requests, "get", lambda url, **kw: FakeResp(200, {"status": "scored", "score": 72.0})
    )
    assert sub.fetch("S9")["score"] == 72.0
    assert sub.history()[-1]["status"] == "scored"
    assert (Path(entry["dir"]) / "final.json").exists()
    assert "72.0" in md.read_text(encoding="utf-8")


def test_failed_attempt_does_not_block_resending_same_file(tmp_path, monkeypatch):
    from closebooks.bankrec import output

    f, _ = _setup(tmp_path, monkeypatch)
    data = tmp_path / "data"
    (data / "live").mkdir(parents=True)
    (data / "live" / "bank_transactions.csv.gz").write_bytes(b"")
    monkeypatch.setattr(
        output, "validate", lambda *a: {"ok": True, "rows": 1, "txns_covered": 1, "txns_total": 1}
    )
    sha = sub._sha(f)
    sub._log({"phase": "LIVE", "sha256": sha, "status": "upload-url-failed"})
    assert sub.preflight(f, "LIVE", data)["sha256"] == sha  # 429 & co. do not block
    sub._log({"phase": "LIVE", "sha256": sha, "status": "scored"})
    with pytest.raises(sub.SubmitError, match="already submitted"):
        sub.preflight(f, "LIVE", data)


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
