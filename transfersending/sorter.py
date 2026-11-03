"""Decide dove salvare ogni file ricevuto.

Le foto e i video vanno in  <dispositivo>/<Anno>/<MM - Mese>/  in base alla
data di SCATTO (letta dentro il file), non alla data del trasferimento.
"""

from __future__ import annotations

import logging
import re
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import exifread

logging.getLogger("exifread").setLevel(logging.ERROR)

MESI = [
    "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
]

IMAGE_EXT = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp", ".tif",
    ".tiff", ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".avif",
}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm"}
DOC_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf",
    ".odt", ".ods", ".odp", ".csv", ".pages", ".numbers", ".key", ".epub",
    ".md", ".json", ".xml", ".zip", ".rar", ".7z", ".vcf", ".ics", ".eml",
}

FOLDER_DOCS = "Documenti"
FOLDER_SCREENSHOT = "Screenshot"
FOLDER_NODATE = "Senza data"
FOLDER_OTHER = "Altri file"

_MP4_EPOCH_OFFSET = 2082844800  # secondi tra 1904-01-01 e 1970-01-01

# Date nel nome del file: IMG-20190512-WA0003, PXL_20230101_123456789,
# Screenshot_2023-01-01-12-34-56, "2019-05-12 14.32.05", signal-2023-01-01-123456 ...
_NAME_DATE = re.compile(
    r"(?<!\d)(19[89]\d|20\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[ _\-T.]{0,3}([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d)|(?!\d))"
)
_SCREENSHOT_NAME = re.compile(r"screen ?shot|schermata|bildschirmfoto|captura", re.I)


def _plausible(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.year < 1980 or dt > datetime.now() + timedelta(days=2):
        return None
    return dt


def exif_date(path: Path) -> tuple[Optional[datetime], bool]:
    """Ritorna (data di scatto, è_screenshot) leggendo i dati EXIF."""
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return None, False
    shot = False
    comment = tags.get("EXIF UserComment")
    if comment is not None and "screenshot" in str(comment).lower():
        shot = True
    for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
        val = tags.get(key)
        if val is None:
            continue
        txt = str(val).strip().replace("\x00", "")
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
            try:
                return _plausible(datetime.strptime(txt[:19], fmt)), shot
            except ValueError:
                continue
    return None, shot


def video_date(path: Path) -> Optional[datetime]:
    """Legge la data di registrazione dal box 'mvhd' di MP4/MOV (in UTC)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            moov = _find_box(f, 0, end, b"moov")
            if moov is None:
                return None
            mvhd = _find_box(f, moov[0], moov[1], b"mvhd")
            if mvhd is None:
                return None
            f.seek(mvhd[0])
            version = f.read(4)[0]
            if version == 1:
                secs = struct.unpack(">Q", f.read(8))[0]
            else:
                secs = struct.unpack(">I", f.read(4))[0]
    except Exception:
        return None
    if secs <= _MP4_EPOCH_OFFSET:
        return None
    utc = datetime.fromtimestamp(secs - _MP4_EPOCH_OFFSET, tz=timezone.utc)
    return _plausible(utc.astimezone().replace(tzinfo=None))


def _find_box(f, start: int, end: int, name: bytes):
    """Cerca un box ISO-BMFF tra start e end; ritorna (inizio_dati, fine)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return None
        size, kind = struct.unpack(">I4s", header)
        data = pos + 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            data = pos + 16
        elif size == 0:
            size = end - pos
        if size < 8:
            return None
        if kind == name:
            return data, pos + size
        pos += size
    return None


def name_date(name: str) -> tuple[Optional[datetime], bool]:
    """Cerca una data nel nome del file. Ritorna (data, contiene_orario)."""
    m = _NAME_DATE.search(name)
    if not m:
        return None, False
    y, mo, d, h, mi, s = m.groups()
    try:
        if h is not None:
            return _plausible(datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))), True
        return _plausible(datetime(int(y), int(mo), int(d))), False
    except ValueError:
        return None, False


class Placement:
    """Risultato: cartella relativa e nome finale del file."""

    def __init__(self, folder: Path, filename: str, taken: Optional[datetime], kind: str):
        self.folder = folder
        self.filename = filename
        self.taken = taken
        self.kind = kind  # foto | video | screenshot | documento | senza_data | altro

    def __repr__(self) -> str:
        return f"Placement({self.folder / self.filename!s}, {self.kind})"


def month_folder(dt: datetime) -> Path:
    return Path(f"{dt.year:04d}") / f"{dt.month:02d} - {MESI[dt.month - 1]}"


def safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:180] or "file"


def classify(path: Path, original_name: str) -> Placement:
    """Sceglie cartella e nome per un file già scritto su disco in `path`."""
    name = safe_name(original_name)
    ext = Path(name).suffix.lower()

    if ext in DOC_EXT:
        return Placement(Path(FOLDER_DOCS), name, None, "documento")

    if ext in IMAGE_EXT:
        taken, is_shot = exif_date(path)
        with_time = taken is not None
        if taken is None:
            taken, with_time = name_date(name)
        if is_shot or _SCREENSHOT_NAME.search(name) or (ext == ".png" and not with_time and taken is None):
            folder = Path(FOLDER_SCREENSHOT)
            if taken:
                folder = folder / f"{taken.year:04d}"
            return Placement(folder, _prefixed(name, taken, with_time), taken, "screenshot")
        kind = "foto"
    elif ext in VIDEO_EXT:
        taken = video_date(path)
        with_time = taken is not None
        if taken is None:
            taken, with_time = name_date(name)
        kind = "video"
    else:
        return Placement(Path(FOLDER_OTHER), name, None, "altro")

    if taken is None:
        return Placement(Path(FOLDER_NODATE), name, None, "senza_data")
    return Placement(month_folder(taken), _prefixed(name, taken, with_time), taken, kind)


def _prefixed(name: str, dt: Optional[datetime], with_time: bool) -> str:
    if dt is None:
        return name
    prefix = dt.strftime("%Y-%m-%d_%H-%M-%S") if with_time else dt.strftime("%Y-%m-%d")
    if name.startswith(prefix):
        return name
    return f"{prefix}_{name}"
