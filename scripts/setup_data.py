"""Deja data/challenge/ listo en un comando: descarga (o copia) los paquetes oficiales,
verifica su SHA-256 y los extrae. Opcionalmente instala la caché de texto de PDFs.

    uv run python scripts/setup_data.py                       # descarga de hackathon.usetalky.com
    uv run python scripts/setup_data.py --from D:/closebooks  # paquetes ya bajados (USB/Drive)
    uv run python scripts/setup_data.py --cache closebooks-pdf-cache.zip
    uv run python scripts/setup_data.py --final-sha <sha256>    # a las 18:00, añade el FINAL

Idempotente: lo ya verificado y extraído no se repite.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "challenge"
DOWNLOADS = DATA / "_downloads"
BASE_URL = "https://hackathon.usetalky.com/data/v1/"

# name -> sha256 (publicados en https://hackathon.usetalky.com/datos)
PACKAGES = {
    "bankrec-docs-and-masters.zip": (
        "210b2404efd011af248a251db2bc0ee85a2ddaa1d56a7a0ca0b95ede18cc1071"
    ),
    "bankrec-train.tar.gz": "163f51a818193f6310b6fba96e3830e4347c883b0b84bc070153f5018e3f06b8",
    "bankrec-live.tar.gz": "eec490d7c605048d0d3a8400af97ee13c9fe49e68dfeb3d68546daf50d8af520",
}
FINAL = "bankrec-final.tar.gz"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(name: str, expected: str, source: Path | None) -> Path:
    dest = DOWNLOADS / name
    if dest.exists() and sha256(dest) == expected:
        print(f"  {name}: ya descargado y verificado")
        return dest
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if source is not None:
        src = source / name
        if not src.exists():
            sys.exit(f"ERROR: falta {src}")
        print(f"  {name}: copiando desde {src}")
        shutil.copyfile(src, tmp)
    else:
        print(f"  {name}: descargando {BASE_URL + name}")
        with urllib.request.urlopen(BASE_URL + name, timeout=60) as resp, tmp.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r    {done / total:6.1%} de {total / 1e6:.0f} MB", end="", flush=True)
            print()
    got = sha256(tmp)
    if got != expected:
        tmp.unlink()
        sys.exit(f"ERROR: SHA-256 de {name} no coincide ({got[:12]}… ≠ {expected[:12]}…)")
    tmp.replace(dest)
    print("    SHA-256 OK")
    return dest


def _skip(member_name: str) -> bool:
    # los tar vienen de macOS: ignorar los ficheros AppleDouble "._*"
    return Path(member_name).name.startswith("._")


def extract(archive: Path, expected: str) -> None:
    marker = DOWNLOADS / f"{archive.name}.extracted"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == expected:
        print(f"  {archive.name}: ya extraído")
        return
    print(f"  {archive.name}: extrayendo en {DATA}")
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if not _skip(m)]
            zf.extractall(DATA, members=members)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            members = [m for m in tf.getmembers() if not _skip(m.name)]
            tf.extractall(DATA, members=members, filter="data")
    marker.write_text(expected, encoding="utf-8")


def install_cache(zip_path: Path) -> None:
    target = ROOT / "out" / "cache"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.startswith("pdf_text_") and n.endswith(".json")]
        for n in names:
            (target / n).write_bytes(zf.read(n))
    print(f"  caché de PDFs: {len(names)} ficheros en {target}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--from", dest="source", type=Path, help="carpeta con los paquetes ya bajados")
    ap.add_argument("--final-sha", help="SHA-256 publicado del FINAL (a las 18:00)")
    ap.add_argument("--cache", type=Path, help="zip con la caché de texto de PDFs del equipo")
    ap.add_argument("--skip-train", action="store_true", help="no bajar TRAIN (657 MB)")
    args = ap.parse_args()

    packages = dict(PACKAGES)
    if args.skip_train:
        packages.pop("bankrec-train.tar.gz")
    if args.final_sha:
        packages[FINAL] = args.final_sha.strip().lower()

    print(f"Datos en {DATA}")
    for name, expected in packages.items():
        extract(fetch(name, expected, args.source), expected)
    if args.cache:
        install_cache(args.cache)
    print("Listo. Comprueba con: uv run closebooks info")


if __name__ == "__main__":
    main()
