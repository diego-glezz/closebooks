"""Append-only audit trail (JSONL). Every automated decision writes one event.

An auditor must be able to answer, for any booked or blocked item:
  who/what decided (agent + rule), based on which records (evidence), with what confidence,
  and whether a human must look at it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, list):
        return [_jsonable(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


class AuditTrail:
    def __init__(self, path: Path | None = None, run_id: str | None = None):
        self.run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.path = path
        self.events: list[dict[str, Any]] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        stage: str,
        agent: str,
        action: str,
        subject_refs: list[str],
        rule: str | None = None,
        confidence: float | None = None,
        evidence: list[Any] | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        event = {
            "seq": len(self.events) + 1,
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "stage": stage,
            "agent": agent,
            "action": action,
            "subject_refs": subject_refs,
            "rule": rule,
            "confidence": confidence,
            "evidence": _jsonable(evidence or []),
            **{k: _jsonable(v) for k, v in details.items()},
        }
        self.events.append(event)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return event

    def for_subject(self, ref: str) -> list[dict[str, Any]]:
        return [e for e in self.events if ref in e["subject_refs"]]
