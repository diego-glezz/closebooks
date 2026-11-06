"""Submit to the Talky leaderboard (https://hackathon.usetalky.com/enviar).

Flow (official): POST /api/submissions/upload-url -> POST file to S3 (presigned form) ->
POST /api/submissions/{id}/score -> GET /api/submissions/{id} every >= 5 s until final status.

Safety rails:
  * token only from the HK_TOKEN env var (or .env); never printed or logged
  * the file is validated locally with the official scorer BEFORE asking for an upload URL
    (asking for the URL already consumes a slot: 1 per 3 min, 20 per day per phase)
  * identical file (same sha256) already submitted -> refused unless --force
  * FINAL needs an explicit typed confirmation: ONE scored submission per team
  * every attempt is logged in out/submissions/log.jsonl (+ the full JSON response)
"""

from __future__ import annotations

import hashlib
import json
import os
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
    if not force and any(h.get("sha256") == sha and h.get("phase") == phase for h in history()):
        raise SubmitError("this exact file was already submitted (use --force to resend)")
    if phase == "FINAL" and any(
        h.get("phase") == "FINAL" and h.get("status") in ("scored", "received") for h in history()
    ):
        raise SubmitError("a FINAL submission was already accepted: FINAL allows only one")
    return {"sha256": sha, "rows": v["rows"], "txns": v["txns_total"]}


def submit(path: Path, phase: str, note: str = "", timeout: int = 600) -> dict:
    """Upload + score + poll. Call preflight() first."""
    import requests

    headers = {"Authorization": f"Bearer {_token()}"}
    entry: dict = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "phase": phase,
        "file": str(path),
        "sha256": _sha(path),
        "commit": _git_commit(),
        "note": note,
    }
    r = requests.post(
        f"{API}/submissions/upload-url",
        headers=headers,
        json={"phase": phase, "filename": path.name},
        timeout=30,
    )
    if r.status_code != 201:
        entry.update(status="upload-url-failed", http=r.status_code, body=r.text[:500])
        _log(entry)
        raise SubmitError(f"upload-url failed: {r.status_code} {r.text[:300]}")
    up = r.json()
    sub_id = up["sub_id"]
    entry["sub_id"] = sub_id
    with path.open("rb") as fh:
        s3 = requests.post(
            up["upload"]["url"],
            data=up["upload"]["fields"],
            files={"file": (path.name, fh)},
            timeout=900,
        )
    if s3.status_code not in (200, 201, 204):
        entry.update(status="s3-failed", http=s3.status_code, body=s3.text[:500])
        _log(entry)
        raise SubmitError(f"S3 upload failed: {s3.status_code}")
    r = requests.post(f"{API}/submissions/{sub_id}/score", headers=headers, json={}, timeout=30)
    if r.status_code >= 400:
        entry.update(status="score-failed", http=r.status_code, body=r.text[:500])
        _log(entry)
        raise SubmitError(f"score request failed: {r.status_code} {r.text[:300]}")
    sub = r.json()
    deadline = time.time() + timeout
    while sub.get("status") not in FINAL_STATES and time.time() < deadline:
        time.sleep(POLL_SECONDS)
        sub = requests.get(f"{API}/submissions/{sub_id}", headers=headers, timeout=30).json()
    entry.update(status=sub.get("status"), score=sub.get("score"))
    _log(entry)
    (LOG_DIR / f"{sub_id}.json").write_text(
        json.dumps(sub, indent=1, ensure_ascii=False), encoding="utf-8"
    )
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
