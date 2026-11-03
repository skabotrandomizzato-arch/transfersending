"""Server web: HTTPS per i telefoni, HTTP solo-locale per la pagina del computer."""

from __future__ import annotations

import hmac
import io
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .storage import Storage, UploadError

MAX_CHUNK = 32 * 1024 * 1024


def resource(name: str) -> bytes:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for p in (base / "transfersending" / "web" / name, Path(__file__).parent / "web" / name):
        if p.exists():
            return p.read_bytes()
    raise FileNotFoundError(name)


def lan_ips() -> list[str]:
    """Indirizzi IP privati di questo computer (il primo è quello principale)."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # non invia nulla, serve solo a scegliere l'interfaccia
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    ips = [ip for ip in ips if ipaddress.ip_address(ip).is_private
           and not ipaddress.ip_address(ip).is_loopback]
    return ips or ["127.0.0.1"]


def is_local_client(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class App:
    def __init__(self, dest: Path, idle_minutes: int = 30):
        self.storage = Storage(dest)
        self.token = secrets.token_urlsafe(18)
        self.host_token = secrets.token_urlsafe(18)
        self.idle_seconds = idle_minutes * 60
        self.last_activity = time.time()
        self.stop_event = threading.Event()
        self.phone_url = ""
        self.ips: list[str] = []
        self.port = 0

    def touch(self) -> None:
        self.last_activity = time.time()

    def watchdog(self) -> None:
        """Si spegne da solo dopo un periodo senza attività."""
        while not self.stop_event.wait(30):
            busy = any(time.time() - a["t"] < 120 for a in self.storage.active.values())
            if busy:
                self.touch()
            if self.idle_seconds and time.time() - self.last_activity > self.idle_seconds:
                print("Nessuna attività: spegnimento automatico.")
                self.stop_event.set()


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TransferSending"
    sys_version = ""
    app: App

    def log_message(self, fmt, *args):  # niente log rumorosi
        pass

    def send_json(self, obj, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_page(self, name: str) -> None:
        body = resource(name)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > limit:
            raise UploadError(413, "richiesta troppo grande")
        chunks, left = [], length
        while left:
            data = self.rfile.read(min(left, 1024 * 1024))
            if not data:
                raise UploadError(400, "connessione interrotta")
            chunks.append(data)
            left -= len(data)
        return b"".join(chunks)

    def read_json(self) -> dict:
        try:
            return json.loads(self.read_body(64 * 1024) or b"{}")
        except ValueError:
            raise UploadError(400, "JSON non valido")

    def cookie(self, name: str) -> str:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[name].value if name in c else ""

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()


class PhoneHandler(_Base):
    """Pagina per i telefoni (HTTPS, protetta dal codice segreto del QR)."""

    def authorized(self) -> bool:
        given = self.cookie("ts") or self.headers.get("X-Token", "")
        return bool(given) and hmac.compare_digest(given, self.app.token)

    def guard(self) -> bool:
        if not is_local_client(self.client_address[0]):
            self.send_json({"error": "solo rete locale"}, 403)
            return False
        self.app.touch()
        return True

    def do_GET(self):
        if not self.guard():
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                key = (q.get("k") or [""])[0]
                if key and hmac.compare_digest(key, self.app.token):
                    self.redirect("/", f"ts={self.app.token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=86400")
                elif self.authorized():
                    self.send_page("phone.html")
                else:
                    self.send_page("locked.html")
            elif not self.authorized():
                self.send_json({"error": "codice mancante: inquadra di nuovo il QR"}, 401)
            elif url.path == "/api/library":
                self.send_json(self.app.storage.library())
            elif url.path == "/api/file":
                self.send_file((q.get("p") or [""])[0], download=(q.get("dl") or ["1"])[0] == "1")
            elif url.path == "/api/zip":
                self.send_zip((q.get("p") or [""])[0])
            else:
                self.send_json({"error": "non trovato"}, 404)
        except UploadError as e:
            self.send_json({"error": str(e), **e.extra}, e.status)

    def do_POST(self):
        if not self.guard():
            return
        if not self.authorized():
            self.close_connection = True
            return self.send_json({"error": "codice mancante: inquadra di nuovo il QR"}, 401,
                                  {"Connection": "close"})
        url = urlparse(self.path)
        st = self.app.storage
        try:
            if url.path == "/api/upload/start":
                d = self.read_json()
                self.send_json(st.start(str(d.get("device", "")), str(d.get("name", "")),
                                        int(d.get("size", -1)), int(d.get("lastModified") or 0)))
            elif url.path == "/api/upload/chunk":
                q = parse_qs(url.query)
                uid = (q.get("id") or [""])[0]
                offset = int((q.get("offset") or ["-1"])[0])
                if offset < 0:
                    raise UploadError(400, "posizione mancante")
                data = self.read_body(MAX_CHUNK)
                received = st.write_chunk(uid, offset, data, self.headers.get("X-Chunk-SHA256"))
                self.send_json({"received": received})
            elif url.path == "/api/upload/finish":
                d = self.read_json()
                self.send_json(st.finish(str(d.get("id", ""))))
            else:
                self.send_json({"error": "non trovato"}, 404)
        except UploadError as e:
            if e.status >= 500 or e.status == 422:
                st.stats["errors"] += 1
            self.close_connection = True
            self.send_json({"error": str(e), **e.extra}, e.status, {"Connection": "close"})
        except (ValueError, KeyError, TypeError):
            self.close_connection = True
            self.send_json({"error": "richiesta non valida"}, 400, {"Connection": "close"})

    def send_file(self, rel: str, download: bool) -> None:
        path = self.app.storage.safe_path(rel)
        if not path.is_file():
            raise UploadError(404, "file non trovato")
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        status = 200
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(status)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        disp = "attachment" if download else "inline"
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left:
                block = f.read(min(left, 1024 * 1024))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)
                self.app.touch()

    def send_zip(self, rel: str) -> None:
        folder = self.app.storage.safe_path(rel)
        if not folder.is_dir():
            raise UploadError(404, "cartella non trovata")
        files = sorted(p for p in folder.rglob("*") if p.is_file()
                       and not any(part.startswith(".") for part in p.relative_to(folder).parts))
        name = "_".join(folder.relative_to(self.app.storage.dest).parts) + ".zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        out = _Unseekable(self.wfile, self.app)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for p in files:
                zf.write(p, p.relative_to(folder).as_posix())


class _Unseekable(io.RawIOBase):
    """Flusso di sola scrittura, così lo zip viene creato mentre si scarica."""

    def __init__(self, raw, app: App):
        self.raw, self.app, self.pos = raw, app, 0

    def writable(self):
        return True

    def write(self, b):
        self.raw.write(b)
        self.pos += len(b)
        self.app.touch()
        return len(b)

    def tell(self):
        return self.pos

    def flush(self):
        self.raw.flush()


class HostHandler(_Base):
    """Pagina del computer: QR, stato, spegnimento. Raggiungibile solo da questo PC."""

    def authorized(self) -> bool:
        return hmac.compare_digest(self.cookie("th") or "-", self.app.host_token)

    def do_GET(self):
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            return self.send_json({"error": "vietato"}, 403)
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/ping":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/":
            key = (q.get("h") or [""])[0]
            if key and hmac.compare_digest(key, self.app.host_token):
                return self.redirect("/", f"th={self.app.host_token}; Path=/; HttpOnly; SameSite=Strict")
            if not self.authorized():
                return self.send_json({"error": "riapri il programma"}, 401)
            return self.send_page("host.html")
        if not self.authorized():
            return self.send_json({"error": "vietato"}, 401)
        if url.path == "/api/status":
            st = self.app.storage
            active = [dict(a) for a in list(st.active.values()) if time.time() - a["t"] < 120]
            self.send_json({
                "url": self.app.phone_url, "dest": str(st.dest), "ips": self.app.ips,
                "port": self.app.port, "stats": st.stats, "recent": st.recent[:20],
                "active": active, "qr": qr_svg(self.app.phone_url),
                "idleMinutes": self.app.idle_seconds // 60,
                "idleLeft": max(0, int(self.app.idle_seconds - (time.time() - self.app.last_activity))),
            })
        else:
            self.send_json({"error": "non trovato"}, 404)

    def do_POST(self):
        if not ipaddress.ip_address(self.client_address[0]).is_loopback or not self.authorized():
            return self.send_json({"error": "vietato"}, 403)
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{self.server.server_address[1]}"):
            return self.send_json({"error": "vietato"}, 403)
        url = urlparse(self.path)
        if url.path == "/api/open-folder":
            open_folder(self.app.storage.dest)
            self.send_json({"ok": True})
        elif url.path == "/api/quit":
            self.send_json({"ok": True})
            threading.Timer(0.5, self.app.stop_event.set).start()
        else:
            self.send_json({"error": "non trovato"}, 404)


_qr_cache: dict[str, str] = {}


def qr_svg(text: str) -> str:
    if text not in _qr_cache:
        import qrcode
        import qrcode.image.svg

        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        _qr_cache[text] = img.to_string(encoding="unicode")
    return _qr_cache[text]


def open_folder(path: Path) -> None:
    import subprocess

    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class TLSServer(ThreadingHTTPServer):
    """La cifratura viene negoziata nel thread di ogni connessione."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, addr, handler, context: ssl.SSLContext):
        self.context = context
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        request.settimeout(90)
        try:
            tls = self.context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # es. il telefono ha rifiutato il certificato: riproverà
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        pass  # connessioni interrotte (Wi-Fi caduta) sono normali


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def bind_phone_server(app: App, cert: Path, key: Path, port: int) -> TLSServer:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    handler = type("H", (PhoneHandler,), {"app": app})
    for p in (port, 0):
        try:
            return TLSServer(("0.0.0.0", p), handler, ctx)
        except OSError:
            continue
    raise OSError("nessuna porta libera")


def bind_host_server(app: App) -> LocalServer:
    handler = type("H", (HostHandler,), {"app": app})
    return LocalServer(("127.0.0.1", 0), handler)
