"""Experiment switches for role B (references and third parties).

Every field defaults to the behaviour of `main` except three switches that are ON after measuring
them: `two_pass_party` (DEV, HOLDOUT +0.02), `ref_cn_subset` (DEV and HOLDOUT) and `ref_po` (DEV).
An empty `CB_FLAGS` reproduces the current engine; to get the old baseline pass
`two_pass_party=0,ref_cn_subset=0,ref_po=0`. A candidate
is described by a JSON object in the `CB_FLAGS` environment variable, which lets a genetic search
launch independent processes with different genomes.
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
    # the customer's PO number cited instead of ours (POLICIES §5.5). ON since 2026-11-07:
    # DEV 86.12 -> 86.24, up in all 6 months, precision unchanged. HOLDOUT pending.
    ref_po: bool = True
    ref_corroborate: bool = False  # weak cited suffixes accepted when they agree on ONE party
    trace_ref: bool = False  # record why _by_reference gave up (debug, no effect on output)
    ref_trust_other: bool = False  # full cited numbers of ONE other party beat the name match
    # complete a truncated citation with items close in issue date. ON since 2026-11-07:
    # DEV 86.24 -> 86.53, HOLDOUT 85.91 -> 86.20 (ref_completed precision 0.991)
    complete_near: bool = True
    # max uncited items added by the completion subset search. 5 since 2026-11-07 (with
    # complete_near): DEV 89.24 -> 89.42, HOLDOUT 89.09 -> 89.27, ref_completed precision 0.961.
    # k=4 is the conservative choice (+0.12 / +0.11, precision 0.98); k=6 not worth it
    # (marginal precision already 0.82 from k=4 to k=5)
    complete_k: int = 5
    complete_pool: int = 25  # open items (closest in issue date) the completion searches
    # completion subsets of this size or more must be the ONLY one adding up to the cents
    # (like unique_subset); 99 = off (first match wins, behaviour of main)
    unique_from_k: int = 99
    # re-try unresolved receipts of a known payer at the END of the pass, against the items
    # still open then; accept only a UNIQUE exact subset (POLICIES §1: each item settled once)
    # ON since 2026-11-07 with second_pass_age=365: DEV 89.42 -> 89.81, HOLDOUT 89.27 -> 89.66,
    # second_pass pair precision 0.929 / 0.925 (without the age filter: 0.81 / 0.76)
    second_pass: bool = True
    second_pass_max: int = 30  # payer pool size limit (2^15 sums per half)
    # measured on DEV: subsets with an invoice open > 365 days are right 34 % of the time
    # (<= 180 days: 94-97 %); dead invoices are left out of the second-pass pool
    second_pass_age: int = 365  # 0 = off; max days between invoice issue and the receipt
    second_pass_kmax: int = 99  # max invoices in the accepted subset
    trust_no_partial: bool = False  # ref_trust_other only for exact / completed sets (no PARTIAL)
    unid_known_payer: bool = False  # payer known with open invoices: no UNIDENTIFIED exception
    # "UNIDENTIFIED selectivo": like unid_known_payer, but cash-like receipts (small or round
    # amount: cash deposits, POLICIES §3) keep the exception. TRAIN: real UNIDENTIFIED median
    # 591 and 22 % round; receipts that pay invoices median 10,049 and 1 % round.
    # ON since 2026-11-07 (merged A+B+C+D code): DEV 88.69 -> 89.22, HOLDOUT 88.56 -> 89.07
    unid_selective: bool = True
    unid_small: int = 1000  # account-currency units under which a receipt is "cash-like"
    # 14-upgrade: the "small" threshold in EUR equivalent (ECB rate). ON: DEV 89.22 -> 89.23,
    # HOLDOUT 89.07 -> 89.08; also keeps the rule sane for the new ZAR subsidiary in FINAL
    unid_small_eur: bool = True
    # 14 fix: a receipt larger than everything the payer has open cannot pay its invoices, so it
    # stays UNIDENTIFIED even with a known payer (POLICIES §3: no open item explains it)
    unid_cap: bool = False
    # 'FV23-03201 03266' -> FV23-03266 (implicit prefix), POLICIES §5.5. ON: DEV +0.01,
    # HOLDOUT +0.01; its pairs have precision 0.996-1.000
    ref_expand: bool = True
    # ref_set_cn never on invoices already partly paid or with a PDF instalment plan: their
    # shortfall is a remainder / instalment / dispute (POLICIES §3), not credit notes
    cn_guard: bool = False
    # --- numeric genes (default = value hard-coded in main)
    subset_recent_days: int = 240
    subset_cap: int = 22
    subset_max_items: int = 5
    party_rank_comb: int = 4  # payers (by name score) that get combinatorial searches
    party_rank_exact: int = 8  # payers considered at all
    fam_parties: int = 3  # payers whose family filters a cited reference
    name_min_len: int = 8  # chars of a truncated name needed to accept it
    stale_days: int = 0  # 0 = off; ignore open items older than this when completing FIFO
    # cited invoices paid net of a SUBSET of the party's credit notes; ON since 7 nov
    # (DEV 85.99 -> 86.06, HOLDOUT 85.68 -> 85.75, pair precision 0.92 / 0.97)
    ref_cn_subset: bool = True
    pdf_exact: bool = False  # PDF clauses only apply to the item with the exact printed number
    pdf_dated: bool = False  # PDF clauses never touch items issued after the PDF's own date


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
