"""Submit to the Talky leaderboard (https://hackathon.usetalky.com/enviar).

Flow (official): POST /api/submissions/upload-url -> POST file to S3 (presigned form) ->
POST /api/submissions/{id}/score -> GET /api/submissions/{id} every >= 5 s until final status.

Safety rails:
  * token only from the HK_TOKEN env var (or .env); never printed or logged
  * the file is validated locally with the official scorer BEFORE asking for an upload URL
    (asking for the URL already consumes a slot: 1 per 3 min, 20 per day per phase)
  * identical file (same sha256) already ACCEPTED by the server -> refused unless --force
    (a failed attempt, e.g. a 429, does not block resending the same file)
  * FINAL needs an explicit typed confirmation: ONE scored submission per team
  * every attempt is logged in out/submissions/log.jsonl and gets its own folder
    out/submissions/<UTC time>_<PHASE>/ with: the exact file sent, the explain.csv.gz of that
    run, every server response in order (responses.jsonl, upload credentials redacted), the
    final result (final.json) and meta.json. Responses are written as they arrive, so if the
    network dies while polling the sub_id is on disk and `closebooks fetch <sub_id>` recovers it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

API = os.environ.get("HK_API", "https://hackathon.usetalky.com/api")
LOG_DIR = Path("out/submissions")
SUBMISSIONS_MD = Path("docs/SUBMISSIONS.md")
FINAL_STATES = {"scored", "received", "invalid", "error"}
# statuses meaning the server took the file: resending the same bytes is pointless
ACCEPTED = {"scored", "received", "invalid"}
_SECRET_KEYS = ("signature", "credential", "policy", "token", "security", "authorization")
POLL_SECONDS = 5  # the whole venue shares one IP: never poll faster


class SubmitError(RuntimeError):
    """A safety check failed; nothing was sent."""


def _token() -> str:
    tok = os.environ.get("HK_TOKEN", "").strip()
    if not tok:
        raise SubmitError("HK_TOKEN is not set (export it or put it in .env; never commit it)")
    return tok


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
            ).stdout.strip()
            or "uncommitted"
        )
    except OSError:
        return "unknown"


def history() -> list[dict]:
    f = LOG_DIR / "log.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line]


def _log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def preflight(path: Path, phase: str, data_root: Path, force: bool = False) -> dict:
    """Checks that cost nothing. Raises SubmitError if the file must not be sent."""
    from closebooks.bankrec.output import validate

    if phase not in ("LIVE", "FINAL"):
        raise SubmitError("phase must be LIVE or FINAL")
    if not path.exists():
        raise SubmitError(f"{path} does not exist")
    size = path.stat().st_size
    if size > 60 * 1024 * 1024:
        raise SubmitError(f"{path} is {size / 1e6:.1f} MB (limit 60 MB)")
    folder = data_root / phase.lower()
    if not (folder / "bank_transactions.csv.gz").exists():
        raise SubmitError(f"no {folder}/bank_transactions.csv.gz to validate against")
    v = validate(data_root, phase.lower(), path)
    if not v["ok"]:
        raise SubmitError(f"invalid file (official validator): {v['errors'][:5]}")
    if v["txns_covered"] != v["txns_total"]:
        raise SubmitError(
            f"file covers {v['txns_covered']} of {v['txns_total']} bank lines; "
            "missing lines count as wrong"
        )
    sha = _sha(path)
    if not force and any(
        h.get("sha256") == sha and h.get("phase") == phase and h.get("status") in ACCEPTED
        for h in history()
    ):
        raise SubmitError("this exact file was already submitted (use --force to resend)")
    if phase == "FINAL" and any(
        h.get("phase") == "FINAL" and h.get("status") in ("scored", "received") for h in history()
    ):
        raise SubmitError("a FINAL submission was already accepted: FINAL allows only one")
    return {"sha256": sha, "rows": v["rows"], "txns": v["txns_total"]}


def _redact(obj):
    """Drop presigned-upload credentials before writing a response to disk."""
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if any(x in k.lower() for x in _SECRET_KEYS) else _redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class _Recorder:
    """Writes every server exchange to <folder>/responses.jsonl as soon as it happens."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def __call__(self, step: str, resp):
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        rec = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "step": step,
            "http": resp.status_code,
            "body": _redact(body),
        }
        with (self.folder / "responses.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return body

    def write(self, name: str, obj) -> None:
        (self.folder / name).write_text(
            json.dumps(_redact(obj), indent=1, ensure_ascii=False), encoding="utf-8"
        )


def _snapshot(path: Path, folder: Path) -> None:
    """Keep the exact bytes sent and the rule-per-line explanation of that run."""
    shutil.copy2(path, folder / path.name)
    explain = path.parent / "explain.csv.gz"
    if explain.exists() and explain.name != path.name:
        shutil.copy2(explain, folder / explain.name)


def _poll(sub_id: str, sub: dict, headers: dict, rec: _Recorder, timeout: int) -> dict:
    import requests

    deadline = time.time() + timeout
    last = sub.get("status")
    while sub.get("status") not in FINAL_STATES and time.time() < deadline:
        time.sleep(POLL_SECONDS)
        r = requests.get(f"{API}/submissions/{sub_id}", headers=headers, timeout=30)
        body = r.json()
        if body.get("status") != last or body.get("status") in FINAL_STATES:
            rec("poll", r)  # only status changes, not 100 identical "queued"
            last = body.get("status")
        sub = body
    return sub


def submit(path: Path, phase: str, note: str = "", timeout: int = 600) -> dict:
    """Upload + score + poll, saving everything sent and received. Call preflight() first."""
    import requests

    headers = {"Authorization": f"Bearer {_token()}"}
    now = datetime.now(UTC)
    folder = LOG_DIR / f"{now:%Y%m%dT%H%M%SZ}_{phase}"
    rec = _Recorder(folder)
    entry: dict = {
        "ts": now.isoformat(timespec="seconds"),
        "phase": phase,
        "file": str(path),
        "sha256": _sha(path),
        "commit": _git_commit(),
        "note": note,
        "dir": str(folder),
    }
    _snapshot(path, folder)

    def fail(status: str, msg: str, resp=None) -> SubmitError:
        entry.update(status=status)
        if resp is not None:
            entry.update(http=resp.status_code, body=resp.text[:500])
        rec.write("meta.json", entry)
        _log(entry)
        return SubmitError(f"{msg} (todo guardado en {folder})")

    r = requests.post(
        f"{API}/submissions/upload-url",
        headers=headers,
        json={"phase": phase, "filename": path.name},
        timeout=30,
    )
    up = rec("upload-url", r)
    if r.status_code != 201 or not isinstance(up, dict):
        raise fail("upload-url-failed", f"upload-url failed: {r.status_code} {r.text[:300]}", r)
    sub_id = up["sub_id"]
    entry["sub_id"] = sub_id
    rec.write("meta.json", entry)
    with path.open("rb") as fh:
        s3 = requests.post(
            up["upload"]["url"],
            data=up["upload"]["fields"],
            files={"file": (path.name, fh)},
            timeout=900,
        )
    rec("s3-upload", s3)
    if s3.status_code not in (200, 201, 204):
        raise fail("s3-failed", f"S3 upload failed: {s3.status_code}", s3)
    r = requests.post(f"{API}/submissions/{sub_id}/score", headers=headers, json={}, timeout=30)
    sub = rec("score", r)
    if r.status_code >= 400:
        raise fail("score-failed", f"score request failed: {r.status_code} {r.text[:300]}", r)
    if not isinstance(sub, dict):
        sub = {}  # odd body: the poll below asks for the real status
    try:
        sub = _poll(sub_id, sub, headers, rec, timeout)
    except (requests.RequestException, ValueError) as exc:
        raise fail(
            "poll-error",
            f"lost contact while waiting for the score ({type(exc).__name__}); "
            f"recover it with: closebooks fetch {sub_id}",
        ) from exc
    entry.update(status=sub.get("status"), score=sub.get("score"))
    rec.write("final.json", sub)
    rec.write("meta.json", entry)
    _log(entry)
    (LOG_DIR / f"{sub_id}.json").write_text(
        json.dumps(sub, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    _append_markdown(entry, sub)
    return sub


def fetch(sub_id: str) -> dict:
    """Ask the server again for a submission we already made (a GET: uses no slot) and save it."""
    import requests

    headers = {"Authorization": f"Bearer {_token()}"}
    prev = next((h for h in reversed(history()) if h.get("sub_id") == sub_id), None)
    folder = Path(prev["dir"]) if prev and prev.get("dir") else LOG_DIR / f"fetch_{sub_id}"
    rec = _Recorder(folder)
    r = requests.get(f"{API}/submissions/{sub_id}", headers=headers, timeout=30)
    sub = rec("fetch", r)
    if r.status_code >= 400 or not isinstance(sub, dict):
        raise SubmitError(f"fetch failed: {r.status_code} {r.text[:300]}")
    rec.write("final.json", sub)
    (LOG_DIR / f"{sub_id}.json").write_text(
        json.dumps(sub, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    if prev and prev.get("status") not in FINAL_STATES and sub.get("status") in FINAL_STATES:
        entry = {**prev, "status": sub.get("status"), "score": sub.get("score")}
        entry["fetched"] = datetime.now(UTC).isoformat(timespec="seconds")
        rec.write("meta.json", entry)
        _log(entry)
        _append_markdown(entry, sub)
    return sub


def _append_markdown(entry: dict, sub: dict) -> None:
    md = SUBMISSIONS_MD
    if not md.exists():
        return
    m = sub.get("metrics") or {}
    row = (
        f"| {entry['ts'][11:16]} | {entry['phase']} | {entry['commit']} | {entry['note']} | "
        f"{sub.get('score', '')} | {m.get('allocation_f1', '')} | {m.get('txn_accuracy', '')} | "
        f"{m.get('exception_macro_f1', '')} | {sub.get('status', '')} |\n"
    )
    with md.open("a", encoding="utf-8") as fh:
        fh.write(row)
