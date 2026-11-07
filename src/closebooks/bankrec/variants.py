"""Experiment switches for role B (references and third parties).

Every field defaults to the behaviour of `main`, so an empty `CB_FLAGS` reproduces the baseline
byte for byte. A candidate is described by a JSON object in the `CB_FLAGS` environment variable,
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
    two_pass_party: bool = False  # exact/discount/plan across all payers before FIFO/subset
    ref_fuzzy: bool = False  # cited number with one wrong / transposed digit, same payer
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
