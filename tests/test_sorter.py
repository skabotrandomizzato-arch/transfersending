import calendar
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from transfersending import sorter
from transfersending.storage import Storage, UploadError

Image = pytest.importorskip("PIL.Image")


def jpg(path: Path, when, color=(10, 10, 10)):
    im = Image.new("RGB", (16, 16), color)
    ex = im.getexif()
    if when:
        ex.get_ifd(0x8769)[36867] = when
    im.save(path, exif=ex)


def mov(path: Path, local: datetime):
    """Video finto con data di registrazione `local` (ora locale del computer)."""
    utc = local.astimezone(timezone.utc)
    t = calendar.timegm(utc.timetuple()) + 2082844800
    mvhd = struct.pack(">I4sB3sII", 20, b"mvhd", 0, b"\0\0\0", t, t)
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    path.write_bytes(struct.pack(">I4s4sI", 16, b"ftyp", b"qt  ", 0) + moov)


def test_exif_date_wins_over_name(tmp_path):
    p = tmp_path / "a.jpg"
    jpg(p, "2019:05:12 14:32:05")
    pl = sorter.classify(p, "IMG-20200101-WA0001.jpg")
    assert pl.folder == Path("2019/05 - Maggio")
    assert pl.filename == "2019-05-12_14-32-05_IMG-20200101-WA0001.jpg"


@pytest.mark.parametrize("name,expected", [
    ("IMG-20180807-WA0003.jpg", datetime(2018, 8, 7)),
    ("PXL_20230101_123456789.jpg", datetime(2023, 1, 1, 12, 34, 56)),
    ("Screenshot_2023-01-02-10-11-12.png", datetime(2023, 1, 2, 10, 11, 12)),
    ("2019-05-12 14.32.05.jpg", datetime(2019, 5, 12, 14, 32, 5)),
    ("IMG_1234.jpg", None),
    ("foto_99999999.jpg", None),
])
def test_name_dates(name, expected):
    assert sorter.name_date(name)[0] == expected


def test_no_date_goes_to_senza_data(tmp_path):
    p = tmp_path / "x.jpg"
    jpg(p, None)
    assert sorter.classify(p, "foto.jpg").folder == Path("Senza data")


def test_png_without_date_is_screenshot(tmp_path):
    p = tmp_path / "x.png"
    Image.new("RGB", (8, 8)).save(p)
    assert sorter.classify(p, "IMG_9999.PNG").folder == Path("Screenshot")


def test_documents_and_other(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(b"x")
    assert sorter.classify(p, "Contratto.PDF").folder == Path("Documenti")
    assert sorter.classify(p, "archivio.xyz").folder == Path("Altri file")


def test_video_date(tmp_path):
    p = tmp_path / "v.mov"
    mov(p, datetime(2021, 3, 15, 10, 0, 0))
    assert sorter.video_date(p) == datetime(2021, 3, 15, 10, 0, 0)
    assert sorter.classify(p, "IMG_5.MOV").folder == Path("2021/03 - Marzo")


def test_unsafe_names():
    assert sorter.safe_name("../../etc/passwd") == "passwd"
    assert sorter.safe_name("a<b>:c?.jpg") == "a_b__c_.jpg"


def _upload(st: Storage, src: Path, name: str, device="iPhone"):
    data = src.read_bytes()
    r = st.start(device, name, len(data), 1)
    st.write_chunk(r["id"], 0, data, None)
    return st.finish(r["id"])


def test_live_photo_dedup_and_no_overwrite(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    st = Storage(dest)
    photo, video = tmp_path / "p.jpg", tmp_path / "v.mov"
    jpg(photo, "2019:05:12 14:32:05")
    mov(video, datetime(2019, 5, 12, 14, 32, 7))

    a = _upload(st, photo, "IMG_0001.JPG")
    b = _upload(st, video, "IMG_0001.MOV")
    assert a["status"] == b["status"] == "saved"
    assert Path(a["path"]).parent == Path(b["path"]).parent
    assert Path(b["path"]).name == "2019-05-12_14-32-05_IMG_0001.MOV"

    # stesso contenuto: non viene copiato di nuovo
    assert _upload(st, photo, "IMG_0001.JPG")["status"] == "duplicate"

    # stesso nome ma contenuto diverso: nuovo file, l'originale resta intatto
    other = tmp_path / "o.jpg"
    jpg(other, "2019:05:12 14:32:05", color=(200, 0, 0))
    c = _upload(st, other, "IMG_0001.JPG")
    assert c["status"] == "saved" and c["path"] != a["path"]
    assert (dest / a["path"]).read_bytes() == photo.read_bytes()


def test_bad_chunk_and_incomplete_file_rejected(tmp_path):
    st = Storage(tmp_path)
    r = st.start("x", "a.pdf", 4, 0)
    with pytest.raises(UploadError) as e:
        st.write_chunk(r["id"], 0, b"abcd", "00" * 32)
    assert e.value.status == 422
    st.write_chunk(r["id"], 0, b"ab", None)
    with pytest.raises(UploadError) as e:
        st.finish(r["id"])
    assert e.value.status == 409
    # ripresa: il server ricorda quanto è già arrivato
    assert st.start("x", "a.pdf", 4, 0)["received"] == 2


def test_path_traversal_blocked(tmp_path):
    st = Storage(tmp_path)
    for bad in ("../x", "/etc/passwd", ".transfersending/indice.json"):
        with pytest.raises(UploadError):
            st.safe_path(bad)
