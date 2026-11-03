"""Ricezione a pezzi, verifica e archiviazione dei file.

Regole di sicurezza dei dati:
- un file viene spostato nella cartella finale solo dopo che la sua
  dimensione e la sua impronta SHA-256 sono state verificate;
- nessun file esistente viene mai sovrascritto;
- i file già ricevuti (stessa impronta) non vengono copiati due volte.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import sorter

STATE_DIR = ".transfersending"
LIVE_PHOTO_WINDOW = 10  # secondi: foto e video con lo stesso nome = Live Photo


class UploadError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


class Storage:
    def __init__(self, dest: Path):
        self.dest = dest
        self.state = dest / STATE_DIR
        self.incoming = self.state / "in-arrivo"
        self.incoming.mkdir(parents=True, exist_ok=True)
        self.index_path = self.state / "indice.json"
        self.lock = threading.RLock()
        self.file_locks: dict[str, threading.Lock] = {}
        self.index = self._load_index()
        self.stats = {"saved": 0, "duplicates": 0, "bytes": 0, "errors": 0}
        self.recent: list[dict] = []
        self.active: dict[str, dict] = {}

    # ---------- indice (impronte e Live Photo) ----------

    def _load_index(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
            data.setdefault("hashes", {})
            data.setdefault("stems", {})
            return data
        except (OSError, ValueError):
            return {"hashes": {}, "stems": {}}

    def _save_index(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.index_path)

    # ---------- caricamento a pezzi ----------

    @staticmethod
    def upload_id(device: str, name: str, size: int, last_modified: int) -> str:
        key = f"{device}\n{name}\n{size}\n{last_modified}".encode()
        return hashlib.sha256(key).hexdigest()[:32]

    def _paths(self, uid: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[0-9a-f]{32}", uid):
            raise UploadError(400, "id non valido")
        return self.incoming / f"{uid}.part", self.incoming / f"{uid}.json"

    def _flock(self, uid: str) -> threading.Lock:
        with self.lock:
            return self.file_locks.setdefault(uid, threading.Lock())

    def start(self, device: str, name: str, size: int, last_modified: int) -> dict:
        device = sorter.safe_name(device or "Dispositivo")[:60]
        name = sorter.safe_name(name)
        if size < 0:
            raise UploadError(400, "dimensione non valida")
        free = shutil.disk_usage(self.dest).free
        uid = self.upload_id(device, name, size, last_modified)
        part, meta = self._paths(uid)
        with self._flock(uid):
            if not meta.exists():
                meta.write_text(json.dumps({
                    "device": device, "name": name, "size": size,
                    "lastModified": last_modified,
                }), "utf-8")
                part.touch()
            received = part.stat().st_size
        if size - received > free - 200 * 1024 * 1024:
            raise UploadError(507, "Spazio su disco insufficiente sul computer")
        with self.lock:
            self.active[uid] = {"name": name, "device": device, "size": size,
                                "received": received, "t": time.time()}
        return {"id": uid, "received": received}

    def write_chunk(self, uid: str, offset: int, data: bytes, sha: Optional[str]) -> int:
        part, meta = self._paths(uid)
        if not meta.exists():
            raise UploadError(404, "caricamento sconosciuto")
        if sha and hashlib.sha256(data).hexdigest() != sha.lower():
            raise UploadError(422, "pezzo danneggiato durante il trasferimento")
        info = json.loads(meta.read_text("utf-8"))
        with self._flock(uid):
            current = part.stat().st_size
            if offset > current:
                raise UploadError(409, "posizione errata", received=current)
            if offset + len(data) > info["size"]:
                raise UploadError(400, "il pezzo supera la dimensione del file")
            with open(part, "r+b") as f:
                f.truncate(offset)  # se un pezzo era rimasto a metà, lo riscriviamo
                f.seek(offset)
                f.write(data)
            received = offset + len(data)
        with self.lock:
            if uid in self.active:
                self.active[uid]["received"] = received
                self.active[uid]["t"] = time.time()
        return received

    def finish(self, uid: str) -> dict:
        part, meta = self._paths(uid)
        if not meta.exists():
            raise UploadError(404, "caricamento sconosciuto")
        info = json.loads(meta.read_text("utf-8"))
        with self._flock(uid):
            size = part.stat().st_size
            if size != info["size"]:
                raise UploadError(409, "file incompleto", received=size)
            with open(part, "rb+") as f:
                os.fsync(f.fileno())
            digest = _sha256_file(part)
            result = self._place(part, info, digest)
            meta.unlink(missing_ok=True)
        with self.lock:
            self.active.pop(uid, None)
            self.file_locks.pop(uid, None)
        return result

    def _place(self, part: Path, info: dict, digest: str) -> dict:
        with self.lock:
            existing = self.index["hashes"].get(digest)
            if existing and (self.dest / existing).exists():
                part.unlink(missing_ok=True)
                self.stats["duplicates"] += 1
                self._remember(info["name"], existing, "duplicato")
                return {"status": "duplicate", "path": existing, "sha256": digest}

            pl = sorter.classify(part, info["name"])
            folder = Path(info["device"]) / pl.folder
            filename = pl.filename
            paired = False
            stem_key = f"{info['device']}|{Path(info['name']).stem.lower()}"
            if pl.kind in ("foto", "video", "senza_data"):
                pair = self.index["stems"].get(stem_key)
                this_kind = "video" if Path(info["name"]).suffix.lower() in sorter.VIDEO_EXT else "foto"
                if pair and pair.get("kind") != this_kind and _same_moment(pair.get("t"), pl.taken):
                    folder = Path(pair["folder"])
                    filename = pair["prefix"] + sorter.safe_name(info["name"])
                    paired = True

            target_dir = self.dest / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            target = _free_path(target_dir / filename)
            os.replace(part, target)
            _keep_dates(target, info.get("lastModified"), pl.taken)

            rel = target.relative_to(self.dest).as_posix()
            self.index["hashes"][digest] = rel
            if pl.kind in ("foto", "video") and not paired:
                prefix = filename[: len(filename) - len(sorter.safe_name(info["name"]))]
                self.index["stems"][stem_key] = {
                    "folder": folder.as_posix(), "prefix": prefix, "kind": pl.kind,
                    "t": pl.taken.isoformat() if pl.taken else None,
                }
            self._save_index()
            self.stats["saved"] += 1
            self.stats["bytes"] += info["size"]
            self._remember(info["name"], rel, "salvato")
            return {"status": "saved", "path": rel, "sha256": digest}

    def _remember(self, name: str, rel: str, status: str) -> None:
        self.recent.insert(0, {"name": name, "path": rel, "status": status,
                               "t": time.time()})
        del self.recent[50:]

    def cleanup_stale(self, max_age_days: int = 7) -> None:
        """Elimina i caricamenti lasciati a metà da più di una settimana."""
        limit = time.time() - max_age_days * 86400
        for f in self.incoming.iterdir():
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass

    # ---------- lettura per la pagina "Ricevi" ----------

    def safe_path(self, rel: str) -> Path:
        target = (self.dest / rel).resolve()
        root = self.dest.resolve()
        if target != root and root not in target.parents:
            raise UploadError(403, "percorso non consentito")
        if STATE_DIR in target.relative_to(root).parts:
            raise UploadError(403, "percorso non consentito")
        return target

    def library(self) -> list[dict]:
        """Albero: dispositivo -> cartelle (anno/mese, Documenti...) -> file."""
        out = []
        root = self.dest
        for dev in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            groups = []
            for dirpath, dirnames, filenames in os.walk(dev):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                files = sorted(f for f in filenames if not f.startswith("."))
                if not files:
                    continue
                d = Path(dirpath)
                groups.append({
                    "path": d.relative_to(root).as_posix(),
                    "label": " / ".join(d.relative_to(dev).parts) or "(principale)",
                    "files": [{"name": f, "size": (d / f).stat().st_size} for f in files],
                })
            groups.sort(key=_group_sort_key, reverse=True)
            out.append({"device": dev.name, "groups": groups})
        return out


def _group_sort_key(g: dict):
    label = g["label"]
    return (1 if re.match(r"\d{4}", label) else 0, label)


def _same_moment(saved_iso: Optional[str], taken: Optional[datetime]) -> bool:
    if taken is None or saved_iso is None:
        return True
    try:
        saved = datetime.fromisoformat(saved_iso)
    except ValueError:
        return False
    return abs((saved - taken).total_seconds()) <= LIVE_PHOTO_WINDOW


def _free_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 1
    while True:
        cand = target.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def _keep_dates(path: Path, last_modified_ms, taken: Optional[datetime]) -> None:
    """Imposta la data del file alla data di scatto (o a quella originale)."""
    ts = None
    if taken is not None:
        try:
            ts = taken.timestamp()
        except (OverflowError, OSError, ValueError):
            ts = None
    if ts is None and last_modified_ms:
        ts = last_modified_ms / 1000
    if ts:
        try:
            os.utime(path, (ts, ts))
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
