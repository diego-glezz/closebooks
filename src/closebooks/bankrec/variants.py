"""Experiment switches for role B (references and third parties).

Every field defaults to the behaviour of `main` except `two_pass_party`, which is on (measured
gain on DEV). To reproduce the old baseline pass `two_pass_party=0`. A candidate is described by a JSON object in the `CB_FLAGS` environment variable,
which lets a genetic search launch independent processes with different genomes.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields


@dataclass
class Flags:
    # --- algorithms (booleans)
    iban_learn: bool = False  # learn payer IBANs from confidently matched receipts
    name_fuzzy: bool = False  # party by name: typos in the first word / words out of order
    # exact/discount/plan across all payers before FIFO/subset. ON by default since 2026-11-07:
    # DEV 2024-H2 85.99 -> 86.04 (party_subset precision 0.932 -> 0.944). HOLDOUT pending.
    two_pass_party: bool = True
    ref_fuzzy: bool = False  # cited number with one wrong / transposed digit, same payer
    # --- strategies from the TRAIN diagnosis (see scripts/analyze_b.py, diagnose_*.py)
    fifo_subset: bool = False  # many-invoice payment: subset closest to FIFO order wins
    fs_cap: int = 30  # oldest open items of the payer considered (2^15 sums per half)
    fs_max_holes: int = 2  # unpaid older invoices tolerated inside the chosen subset
    exact_recent_days: int = 0  # 0 = off; same-amount candidates: skip invoices older than this
    # --- numeric genes (default = value hard-coded in main)
    subset_recent_days: int = 240
    subset_cap: int = 22
    subset_max_items: int = 5
    party_rank_comb: int = 4  # payers (by name score) that get combinatorial searches
    party_rank_exact: int = 8  # payers considered at all
    fam_parties: int = 3  # payers whose family filters a cited reference
    name_min_len: int = 8  # chars of a truncated name needed to accept it
    stale_days: int = 0  # 0 = off; ignore open items older than this when completing FIFO


def load() -> Flags:
    raw = os.environ.get("CB_FLAGS", "").strip()
    flags = Flags()
    if raw:
        data = json.loads(raw)
        known = {f.name for f in fields(Flags)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown flags: {sorted(unknown)}")
        for k, v in data.items():
            setattr(flags, k, type(getattr(flags, k))(v))
    return flags


FLAGS = load()


def describe() -> str:
    return json.dumps(asdict(FLAGS), sort_keys=True)
