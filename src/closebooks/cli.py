"""closebooks CLI — `uv run closebooks --help`."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, help="Bank reconciliation engine (Talky hackathon).")
console = Console()
DATA = Path("data/challenge")


@app.command()
def run(
    phase: str = typer.Argument(..., help="sample | train | live | final"),
    data: Path = DATA,
    out: Path = typer.Option(None, help="submission path (default out/<phase>/submission.csv.gz)"),
    do_score: bool = typer.Option(True, "--score/--no-score", help="score if a golden exists"),
    start: str = typer.Option("", help="train window start YYYY-MM-DD (LIVE-like evaluation)"),
    end: str = typer.Option("", help="train window end YYYY-MM-DD"),
) -> None:
    """Run the engine on a phase, write the submission, score it (sample/train) or validate it."""
    from closebooks.bankrec.data import load_phase
    from closebooks.bankrec.engine import Engine
    from closebooks.bankrec.output import (
        dump,
        rule_counts,
        score_phase,
        validate,
        write_csv,
        write_explain,
    )

    steps = _Steps(6)
    t0 = time.time()
    if start and end:
        from closebooks.bankrec.data import load_window

        steps(f"cargar datos: ventana de TRAIN {start} → {end}")
        ph = load_window(data, start, end)
        phase = ph.name
    else:
        steps(f"cargar datos: fase {phase}")
        ph = load_phase(data, phase)
    t1 = time.time()
    steps("modelo de categorías (aprendido de TRAIN; se reutiliza si ya existe)")
    cat_model = _category_model(data, ph.name, start)
    steps(f"preparar el motor ({len(ph.bank):,} movimientos)".replace(",", "."))
    eng = Engine(ph, category_model=cat_model, on_step=steps.sub)
    t2 = time.time()
    steps("emparejar movimientos con partidas")
    results = _run_with_progress(eng)
    t3 = time.time()
    out = out or Path("out") / phase / "submission.csv.gz"
    steps(f"escribir {out} y explain.csv.gz")
    write_csv(results, out)
    write_explain(results, out.parent / "explain.csv.gz")
    scored = ph.golden is not None and do_score
    steps("puntuar con el evaluador oficial" if scored else "validar con el evaluador oficial")
    res = score_phase(ph, out) if scored else validate(data, phase, out)
    if scored:
        dump(res, out.parent / "score.json")
    steps.done()
    console.print(
        f"load {t1 - t0:.1f}s · setup {t2 - t1:.1f}s · match {t3 - t2:.1f}s · "
        f"{len(results)} lines -> {out}"
    )
    console.print(dict(rule_counts(results).most_common()))
    if scored:
        _print_score(res)
    else:
        console.print(res)


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _say(msg: str) -> None:
    console.print(msg)
    console.file.flush()  # show it now even when stdout is a pipe (Claude Code, logs)


class _Steps:
    """Prints `[i/N] step…` when each step starts and how long the previous one took."""

    def __init__(self, n: int):
        self.n, self.i, self.t0, self.t = n, 0, time.time(), None

    def _close(self) -> None:
        if self.t is not None:
            _say(f"      [dim]hecho en {_fmt(time.time() - self.t)}[/]")

    def __call__(self, msg: str) -> None:
        self._close()
        self.i += 1
        self.t = time.time()
        _say(f"[cyan][{self.i}/{self.n}][/] {msg}…")

    def sub(self, msg: str) -> None:
        _say(f"      · {msg}…")

    def done(self) -> None:
        self._close()
        self.t = None
        _say(f"[green]listo en {_fmt(time.time() - self.t0)}[/]")


def _run_with_progress(eng):
    """Engine.run with a live bar in a terminal, or a line every 10 % when piped."""
    total = len(eng.rows)
    if console.is_terminal:
        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeElapsedColumn,
            TimeRemainingColumn,
        )

        with Progress(
            TextColumn("      "),
            BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TextColumn("quedan"),
            TimeRemainingColumn(),
            console=console,
        ) as prog:
            task = prog.add_task("match", total=total)
            return eng.run(progress=lambda done, _t: prog.update(task, completed=done))

    t0 = time.time()
    next_pct = [10]

    def report(done: int, _total: int) -> None:
        pct = done * 100 // max(total, 1)
        if pct < next_pct[0] and done < total:
            return
        el = time.time() - t0
        rest = el / done * (total - done) if done else 0.0
        line = f"      {pct:3d}% ({done:,}/{total:,}) · {_fmt(el)} · quedan ~{_fmt(rest)}"
        _say(line.replace(",", "."))
        next_pct[0] = (pct // 10 + 1) * 10

    return eng.run(progress=report)


def _category_model(data: Path, phase: str, start: str) -> Path | None:
    """Category model trained on TRAIN lines booked strictly before what we evaluate."""
    from closebooks.bankrec.learn import build

    before = start or {"sample": "2025-06-01", "live": "2025-07-01", "final": "2025-07-01"}.get(
        phase, ""
    )
    if not before or not (data / "train" / "golden_allocations.csv.gz").exists():
        return None
    path = Path("out/models") / f"categories_{before}.json"
    if not path.exists():
        build(data, before, path)
    return path


def _print_score(res: dict) -> None:
    if not res["ok"]:
        console.print("[red]INVALID[/]", res["errors"][:10])
        return
    m = res["metrics"]
    console.print(
        f"[bold]SCORE {res['score']}[/]  alloc_F1={m['allocation_f1']:.3f} "
        f"(P={m['precision']:.3f} R={m['recall']:.3f})  txn_acc={m['txn_accuracy']:.3f}  "
        f"exc_macroF1={m['exception_macro_f1']:.3f}"
    )
    t = Table(title="accuracy by segment (worst first, n>=5)")
    for c in ("segment", "n", "acc"):
        t.add_column(c)
    segs = sorted(
        ((k, v) for k, v in res["segments"].items() if v["n"] >= 5),
        key=lambda kv: kv[1]["accuracy"] * 1e6 - kv[1]["n"],
    )
    for k, v in segs[:30]:
        t.add_row(k, str(v["n"]), f"{v['accuracy']:.2f}")
    console.print(t)


@app.command(name="score")
def score_cmd(submission: Path, phase: str = "sample", data: Path = DATA) -> None:
    """Score an existing submission file against sample/train golden (official scorer)."""
    from closebooks.bankrec.output import score

    _print_score(score(data, phase, submission))


@app.command()
def diff(
    phase: str = "sample",
    data: Path = DATA,
    segment: str = "",
    limit: int = 15,
    start: str = "",
    end: str = "",
    rule_filter: str = typer.Option("", "--rule", help="only lines decided by this rule"),
) -> None:
    """Show lines we got wrong vs the golden, with bank text and the rule we used."""
    import pandas as pd

    from closebooks.bankrec.data import load_phase, load_window

    if start and end:
        ph = load_window(data, start, end)
        phase = ph.name
    else:
        ph = load_phase(data, phase)
    sub = pd.read_csv(Path("out") / phase / "submission.csv.gz", dtype=str, keep_default_na=False)
    exp = pd.read_csv(Path("out") / phase / "explain.csv.gz", dtype=str, keep_default_na=False)
    g = ph.golden
    assert g is not None, "phase has no golden"

    def key(df: pd.DataFrame) -> pd.Series:
        return df.groupby("bank_txn_id").apply(
            lambda x: tuple(sorted(zip(x["item_id"], x["exception"], x["category"], strict=False))),
            include_groups=False,
        )

    gk, sk = key(g), key(sub)
    seg = g.groupby("bank_txn_id")["segment"].first()
    rules = exp.set_index("bank_txn_id")["rule"]
    bad = [
        t
        for t in gk.index
        if sk.get(t) != gk[t]
        and (not segment or seg[t] == segment)
        and (not rule_filter or rules.get(t) == rule_filter)
    ]
    console.print(f"{len(bad)} wrong lines" + (f" in {segment}" if segment else ""))
    bank = ph.bank.set_index("bank_txn_id")
    rule = exp.set_index("bank_txn_id")["rule"]
    items = ph.items.set_index("item_id")
    for t in bad[:limit]:
        b = bank.loc[t]
        console.print(
            f"\n[bold]{t}[/] {seg[t]} rule={rule.get(t)} {b['amount']} {b['currency']} "
            f"| {b['text'][:150]} | ref={b['ref1']} {b['ref2']} | cp={b['counterparty_name']}"
        )
        for side, rows in (("gold", g[g.bank_txn_id == t]), ("ours", sub[sub.bank_txn_id == t])):
            for _, x in rows.iterrows():
                num = items["number"].get(x["item_id"], "") if x["item_id"] else ""
                tot = items["total_c"].get(x["item_id"], 0) / 100 if x["item_id"] else ""
                console.print(
                    f"   {side}: {x['item_id']} {num} amt={x['amount']} (total {tot}) "
                    f"{x['category']} {x['exception']}"
                )


@app.command()
def submit(
    file: Path,
    phase: str = typer.Option("LIVE", help="LIVE or FINAL"),
    note: str = typer.Option("", help="what changed (goes to the log and docs/SUBMISSIONS.md)"),
    data: Path = DATA,
    dry_run: bool = typer.Option(False, "--dry-run", help="only run the free local checks"),
    force: bool = typer.Option(False, help="resend a file with the same sha256"),
) -> None:
    """Validate locally, then upload to the leaderboard and wait for the score."""
    from closebooks.submit import SubmitError, preflight
    from closebooks.submit import submit as do_submit

    phase = phase.upper()
    try:
        pf = preflight(file, phase, data, force=force)
    except SubmitError as exc:
        console.print(f"[red]NOT SENT:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(
        f"preflight OK: {pf['rows']} rows, {pf['txns']} bank lines, sha {pf['sha256'][:12]}"
    )
    if dry_run:
        return
    if phase == "FINAL":
        console.print("[bold red]FINAL: ONE scored submission per team. It cannot be undone.[/]")
        if typer.prompt("Type FINAL to confirm") != "FINAL":
            raise typer.Exit(1)
    elif not typer.confirm("Asking for the upload URL uses one LIVE slot. Send?"):
        raise typer.Exit(1)
    try:
        res = do_submit(file, phase, note)
    except SubmitError as exc:
        console.print(f"[red]FAILED:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print_json(
        json.dumps({k: res.get(k) for k in ("status", "score", "metrics", "errors")})
    )


@app.command()
def fetch(sub_id: str) -> None:
    """Re-download the result of a submission we already made (does NOT use a slot)."""
    from closebooks.submit import SubmitError
    from closebooks.submit import fetch as do_fetch

    try:
        res = do_fetch(sub_id)
    except SubmitError as exc:
        console.print(f"[red]FAILED:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print_json(
        json.dumps({k: res.get(k) for k in ("status", "score", "metrics", "errors")})
    )


@app.command()
def history() -> None:
    """List our submissions (from out/submissions/log.jsonl)."""
    from closebooks.submit import history as hist

    t = Table(title="submissions")
    for c in ("ts", "phase", "commit", "status", "score", "note"):
        t.add_column(c)
    for h in hist():
        t.add_row(
            h.get("ts", ""),
            h.get("phase", ""),
            h.get("commit", ""),
            str(h.get("status")),
            str(h.get("score", "")),
            h.get("note", ""),
        )
    console.print(t)


@app.command(name="report")
def report_cmd(
    phase: str = "sample",
    data: Path = DATA,
    start: str = "",
    end: str = "",
) -> None:
    """Where the points are lost: segments, F1 per exception class, precision per rule."""
    from closebooks.bankrec.analysis import report
    from closebooks.bankrec.data import load_phase, load_window

    ph = load_window(data, start, end) if start and end else load_phase(data, phase)
    assert ph.golden is not None, "phase has no golden"
    rep = report(Path("out") / ph.name, ph.golden)
    m = rep["metrics"]
    console.print(
        f"[bold]SCORE {rep['score']}[/] F1={m['allocation_f1']:.3f} "
        f"P={m['precision']:.3f} R={m['recall']:.3f} txn={m['txn_accuracy']:.3f} "
        f"exc={m['exception_macro_f1']:.3f}"
    )
    t = Table(title="lost lines by segment")
    for c in ("segment", "n", "acc", "lost"):
        t.add_column(c)
    for seg, n, acc, lost in rep["lost_by_segment"][:15]:
        t.add_row(seg, str(n), f"{acc:.2f}", str(lost))
    console.print(t)
    t = Table(title="exception classes (each weighs the same)")
    for c in ("class", "gold", "ours", "P", "R", "F1"):
        t.add_column(c)
    for c, v in rep["exceptions"].items():
        t.add_row(
            c, str(v["gold"]), str(v["ours"]), str(v["precision"]), str(v["recall"]), str(v["f1"])
        )
    console.print(t)
    t = Table(title="pair precision by rule")
    for c in ("rule", "pairs", "precision", "wrong"):
        t.add_column(c)
    for k, v in rep["rules"].items():
        t.add_row(k, str(v["pairs"]), str(v["precision"]), str(v["wrong"]))
    console.print(t)


@app.command()
def info() -> None:
    """Print what data phases are available."""
    for p in ("sample", "train", "live", "final"):
        f = DATA / p / "bank_transactions.csv.gz"
        console.print(f"{p:7s} {'OK' if f.exists() else '--'} {f}")


if __name__ == "__main__":
    app()


__all__ = ["app", "json"]
