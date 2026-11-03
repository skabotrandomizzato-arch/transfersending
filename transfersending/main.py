"""Avvio del programma: crea i server, apre la pagina col QR, aspetta."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .certs import ensure_cert
from .server import App, bind_host_server, bind_phone_server, lan_ips


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "TransferSending"


def default_dest() -> Path:
    docs = Path.home() / "Documents"
    if sys.platform.startswith("win"):
        try:
            import ctypes.wintypes

            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)  # Documenti
            if buf.value:
                docs = Path(buf.value)
        except Exception:
            pass
    return docs / "Trasferimenti"


def read_saved_dest(cfg: Path) -> Path | None:
    f = cfg / "cartella.txt"
    try:
        txt = f.read_text("utf-8").strip()
        return Path(txt) if txt else None
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="TransferSending",
                                 description="Trasferisci foto e file dal telefono al computer.")
    ap.add_argument("--cartella", "--dest", dest="dest", help="dove salvare i file ricevuti")
    ap.add_argument("--porta", "--port", dest="port", type=int, default=8765)
    ap.add_argument("--spegni-dopo", dest="idle", type=int, default=30,
                    help="minuti di inattività prima dello spegnimento (0 = mai)")
    ap.add_argument("--no-browser", action="store_true", help="non aprire la pagina col QR")
    args = ap.parse_args(argv)

    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    _redirect_output_if_windowed(cfg)

    running = _already_running(cfg)
    if running:
        print("Già in esecuzione: riapro la pagina.")
        if not args.no_browser:
            webbrowser.open(running)
        return 0

    dest = Path(args.dest).expanduser() if args.dest else (read_saved_dest(cfg) or default_dest())
    dest.mkdir(parents=True, exist_ok=True)

    app = App(dest, idle_minutes=args.idle)
    app.storage.cleanup_stale()
    app.ips = lan_ips()
    cert, key = ensure_cert(cfg / "certificato", app.ips)

    phone = bind_phone_server(app, cert, key, args.port)
    host = bind_host_server(app)
    app.port = phone.server_address[1]
    app.phone_url = f"https://{app.ips[0]}:{app.port}/?k={app.token}"
    host_url = f"http://127.0.0.1:{host.server_address[1]}/?h={app.host_token}"

    _write_running(cfg, host_url)
    for srv in (phone, host):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=app.watchdog, daemon=True).start()

    print(f"TransferSending {__version__}")
    print(f"Cartella di destinazione: {dest}")
    print(f"Indirizzo per il telefono: {app.phone_url}")
    print(f"Pagina del computer (QR):  {host_url}")
    print("Premi Ctrl+C per spegnere.")
    if not args.no_browser:
        webbrowser.open(host_url)

    try:
        while not app.stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    phone.shutdown()
    host.shutdown()
    (cfg / "in-esecuzione.json").unlink(missing_ok=True)
    print("Spento.")
    return 0


def _already_running(cfg: Path) -> str | None:
    import json
    import urllib.request

    try:
        info = json.loads((cfg / "in-esecuzione.json").read_text("utf-8"))
        url = info["host_url"]
        ping = url.split("/?")[0] + "/ping"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(ping, timeout=2) as r:
            if r.read() == b"ok":
                return url
    except Exception:
        pass
    return None


def _write_running(cfg: Path, host_url: str) -> None:
    import json

    f = cfg / "in-esecuzione.json"
    f.write_text(json.dumps({"host_url": host_url, "pid": os.getpid()}), "utf-8")
    try:
        f.chmod(0o600)
    except OSError:
        pass


def _redirect_output_if_windowed(cfg: Path) -> None:
    """Senza finestra di terminale, i messaggi vanno in un file di log."""
    if sys.stdout is None or sys.stderr is None or not sys.stdout.isatty():
        if getattr(sys, "frozen", False):
            log = open(cfg / "log.txt", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log


if __name__ == "__main__":
    sys.exit(main())
