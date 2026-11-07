"""Ensayo del FINAL: fabrica un paquete bankrec-final.tar.gz a partir de LIVE y un directorio de
datos aparte que lo puede recibir con setup_data.py, sin tocar data/challenge ni out/cache.

    uv run python scripts/make_fake_final.py out/rehearsal
    -> out/rehearsal/pkgs/bankrec-final.tar.gz   (final/ = live/ renombrado + masters_final/ con
       una entidad nueva ZA01 en ZAR y algunos movimientos movidos a su cuenta)
    -> out/rehearsal/data/challenge/             (masters, train, live, starter enlazados)
    imprime el SHA-256 para `setup_data.py --data ... --from ... --final-sha <sha>`
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import sys
import tarfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "challenge"


def link(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        return
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
    except OSError:
        if src.is_dir():
            import subprocess

            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], check=True)
        else:
            shutil.copyfile(src, dst)


def main(out: Path) -> None:
    out = out.resolve()
    pkgs, data = out / "pkgs", out / "data" / "challenge"
    pkgs.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    for name in ("masters", "train", "live", "starter"):
        link(DATA / name, data / name)
    for md in DATA.glob("*.md"):
        link(md, data / md.name)

    # masters_final = masters + entidad nueva ZA01 (ZAR) con una cuenta
    mf = {p.name: p.read_bytes() for p in (DATA / "masters").iterdir() if p.is_file()}
    ent = pd.read_csv(io.BytesIO(mf["entities.csv"]), dtype=str, keep_default_na=False)
    ent = pd.concat(
        [
            ent,
            pd.DataFrame(
                [
                    {
                        "entity": "ZA01",
                        "legal_name": "Valtermia South Africa (Pty) Ltd",
                        "country": "ZA",
                        "functional_currency": "ZAR",
                        "role": "commercial",
                        "in_group_since": "2025-10-01",
                    }
                ]
            ),
        ],
        ignore_index=True,
    ).fillna("")
    acc = pd.read_csv(io.BytesIO(mf["bank_accounts.csv"]), dtype=str, keep_default_na=False)
    new_acc = {c: "" for c in acc.columns}
    new_acc.update(
        {
            "account_id": "ZA01-KWIX-ZAR",
            "entity": "ZA01",
            "bank": "KWIX",
            "bank_name": "Kwix Bank",
            "bic": "KWIXZAJJXXX",
            "currency": "ZAR",
            "iban": "",
            "account_number": "4100123456",
            "purpose": "Cobros / Pagos",
            "statement_format": "camt053",
        }
    )
    acc = pd.concat([acc, pd.DataFrame([new_acc])], ignore_index=True)
    mf["entities.csv"] = ent.to_csv(index=False).encode("utf-8")
    mf["bank_accounts.csv"] = acc.to_csv(index=False).encode("utf-8")

    # final/bank_transactions: LIVE con 40 movimientos movidos a la cuenta ZAR nueva (sin tipo ZAR
    # en fx_rates: el motor no debe romperse por una divisa sin cambio)
    bank = pd.read_csv(DATA / "live" / "bank_transactions.csv.gz", dtype=str, keep_default_na=False)
    pick = bank.index[::5000][:40]
    bank.loc[pick, ["account_id", "entity", "bank", "currency"]] = [
        "ZA01-KWIX-ZAR",
        "ZA01",
        "KWIX",
        "ZAR",
    ]
    bank.loc[pick, "text"] = "KWI " + bank.loc[pick, "text"]
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(bank.to_csv(index=False).encode("utf-8"))

    tgz = pkgs / "bankrec-final.tar.gz"
    with tarfile.open(tgz, "w:gz", compresslevel=1) as tf:
        live = DATA / "live"
        for p in sorted(live.rglob("*")):
            rel = p.relative_to(live).as_posix()
            if p.is_dir() or rel == "bank_transactions.csv.gz":
                continue
            tf.add(p, arcname=f"final/{rel}")

        def add_bytes(name: str, payload: bytes) -> None:
            ti = tarfile.TarInfo(name)
            ti.size = len(payload)
            tf.addfile(ti, io.BytesIO(payload))

        add_bytes("final/bank_transactions.csv.gz", buf.getvalue())
        for name, payload in sorted(mf.items()):
            add_bytes(f"final/masters_final/{name}", payload)
    h = hashlib.sha256(tgz.read_bytes()).hexdigest()
    print(f"paquete: {tgz}\nsha256: {h}\ndatos: {data}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "out/rehearsal"))
