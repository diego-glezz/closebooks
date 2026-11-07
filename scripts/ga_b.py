"""Genetic search over the role-B switches (closebooks.bankrec.variants.Flags).

Generation 0 = baseline + each switch alone + all algorithms + random genomes, so it doubles as
the one-by-one test of every idea. Fitness = official score on the DEV window (TRAIN 2024-H2).
Evaluations run as parallel processes of scripts/exp_b.py and are cached by genome.

    python scripts/ga_b.py --workers 8 --generations 3 --pop 16 --seed 42
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

BOOLS = ["iban_learn", "name_fuzzy", "two_pass_party", "ref_fuzzy"]
# every genome passes ALL keys explicitly, so "baseline" below stays two_pass_party=0 even though
# the engine default is now on
CHOICES: dict[str, list[int]] = {
    "subset_recent_days": [120, 180, 240, 300, 365],
    "subset_cap": [18, 22, 26],
    "subset_max_items": [3, 4, 5, 6],
    "party_rank_comb": [2, 3, 4, 6],
    "party_rank_exact": [4, 8, 12],
    "fam_parties": [1, 2, 3, 4],
    "name_min_len": [6, 7, 8, 10],
    "stale_days": [0, 365, 540, 730],
}
DEFAULTS: dict[str, object] = {
    **dict.fromkeys(BOOLS, False),
    "subset_recent_days": 240,
    "subset_cap": 22,
    "subset_max_items": 5,
    "party_rank_comb": 4,
    "party_rank_exact": 8,
    "fam_parties": 3,
    "name_min_len": 8,
    "stale_days": 0,
}
START, END = "2024-07-01", "2024-12-31"
OUT = Path("out/exp_b")


def key(genome: dict) -> str:
    return json.dumps({k: genome[k] for k in sorted(DEFAULTS)}, sort_keys=True)


def gid(genome: dict) -> str:
    return "g" + hashlib.md5(key(genome).encode()).hexdigest()[:8]


def to_arg(genome: dict) -> str:
    return ",".join(f"{k}={int(v)}" for k, v in sorted(genome.items()))


def folder(name: str, start: str, end: str) -> Path:
    return OUT / f"{name}__{start}__{end}" / "result.json"


class Pool:
    def __init__(self, workers: int, start: str = START, end: str = END, seed_hash: str = "1"):
        self.workers, self.start, self.end, self.seed_hash = workers, start, end, seed_hash
        self.cache: dict[str, dict] = {}

    def evaluate(self, items: list[tuple[str, dict]]) -> dict[str, dict]:
        """items = [(name, genome)]; returns {name: result}."""
        todo = []
        for name, g in items:
            res_path = folder(name, self.start, self.end)
            if res_path.exists():
                self.cache[name] = json.loads(res_path.read_text(encoding="utf-8"))
            elif name not in self.cache:
                todo.append((name, g))
        running: list[tuple[str, subprocess.Popen]] = []
        env = {**os.environ, "PYTHONHASHSEED": self.seed_hash}
        while todo or running:
            while todo and len(running) < self.workers:
                name, g = todo.pop(0)
                cmd = [
                    "uv", "run", "python", "scripts/exp_b.py", "--name", name,
                    "--flags", to_arg(g), "--start", self.start, "--end", self.end,
                ]
                log = open(OUT / f"{name}__{self.start}.log", "w", encoding="utf-8")  # noqa: SIM115
                running.append((name, subprocess.Popen(cmd, env=env, stdout=log, stderr=log)))
                print(f"  launch {name} {to_arg(g)}", flush=True)
            time.sleep(10)
            for name, p in list(running):
                if p.poll() is not None:
                    running.remove((name, p))
                    rp = folder(name, self.start, self.end)
                    if rp.exists():
                        self.cache[name] = json.loads(rp.read_text(encoding="utf-8"))
                        print(f"  done   {name} score={self.cache[name]['score']}", flush=True)
                    else:
                        print(f"  FAILED {name} (see log)", flush=True)
                        self.cache[name] = {"score": -1.0, "failed": True}
        return {n: self.cache[n] for n, _ in items}


def random_genome(rng: random.Random) -> dict:
    g = dict(DEFAULTS)
    for b in BOOLS:
        g[b] = rng.random() < 0.5
    for k, vals in CHOICES.items():
        g[k] = rng.choice(vals)
    return g


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    return {k: (a[k] if rng.random() < 0.5 else b[k]) for k in DEFAULTS}


def mutate(g: dict, rng: random.Random, rate: float = 0.15) -> dict:
    g = dict(g)
    for b in BOOLS:
        if rng.random() < rate:
            g[b] = not g[b]
    for k, vals in CHOICES.items():
        if rng.random() < rate:
            g[k] = rng.choice(vals)
    return g


def singles() -> list[tuple[str, dict]]:
    base = dict(DEFAULTS)
    out: list[tuple[str, dict]] = [("baseline", base)]
    for b in BOOLS:
        out.append((f"only_{b}", {**base, b: True}))
    out.append(("all_algos", {**base, **dict.fromkeys(BOOLS, True)}))
    out.append(("wide_subset", {**base, "subset_recent_days": 365, "subset_max_items": 6, "subset_cap": 26}))
    out.append(("narrow_subset", {**base, "subset_recent_days": 180, "subset_max_items": 4}))
    out.append(("stale_540", {**base, "stale_days": 540}))
    out.append(("rank_comb_6", {**base, "party_rank_comb": 6, "party_rank_exact": 12}))
    out.append(("name_len_6", {**base, "name_min_len": 6}))
    out.append(("fam_1", {**base, "fam_parties": 1}))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--pop", type=int, default=16)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    pool = Pool(a.workers)

    items = singles()
    seen = {key(g) for _, g in items}
    while len(items) < a.pop:
        g = random_genome(rng)
        if key(g) not in seen:
            seen.add(key(g))
            items.append((gid(g), g))
    history: list[dict] = []
    print(f"gen 0: {len(items)} genomes", flush=True)
    scored = pool.evaluate(items)
    population = [(scored[n]["score"], n, g) for n, g in items]
    history.append({"gen": 0, "scores": {n: s for s, n, _ in population}})

    for gen in range(1, a.generations + 1):
        population.sort(key=lambda t: -t[0])
        elite = population[:2]
        parents = population[: max(4, len(population) // 2)]
        children: list[tuple[str, dict]] = []
        tries = 0
        while len(children) < a.pop - len(elite) and tries < 500:
            tries += 1
            p1 = max(rng.sample(parents, 2), key=lambda t: t[0])[2]
            p2 = max(rng.sample(parents, 2), key=lambda t: t[0])[2]
            c = mutate(crossover(p1, p2, rng), rng)
            if key(c) in seen:
                continue
            seen.add(key(c))
            children.append((gid(c), c))
        print(f"gen {gen}: {len(children)} new genomes", flush=True)
        res = pool.evaluate(children)
        population = elite + [(res[n]["score"], n, g) for n, g in children]
        history.append({"gen": gen, "scores": {n: s for s, n, _ in population}})
        best = max(population, key=lambda t: t[0])
        print(f"gen {gen} best {best[1]} = {best[0]}", flush=True)

    allres = {n: {"genome": g, "score": pool.cache[n]["score"]} for n, g in _all(items, history, pool)}
    (OUT / "ga_summary.json").write_text(
        json.dumps({"history": history, "all": allres}, indent=1), encoding="utf-8"
    )
    print("saved out/exp_b/ga_summary.json")


def _all(items, history, pool):
    seen = {}
    for name, g in items:
        seen[name] = g
    # children genomes are recoverable from their result.json
    for name, res in pool.cache.items():
        if name not in seen and "flags" in res:
            seen[name] = res["flags"]
    return list(seen.items())


if __name__ == "__main__":
    sys.exit(main())
