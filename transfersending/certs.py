"""Certificato HTTPS "fatto in casa" per cifrare il traffico nella rete locale."""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def ensure_cert(folder: Path, ips: list[str]) -> tuple[Path, Path]:
    """Riusa il certificato salvato se copre gli IP attuali, altrimenti ne crea uno."""
    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    if cert_path.exists() and key_path.exists() and _still_valid(cert_path, ips):
        return cert_path, key_path

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TransferSending")])
    now = dt.datetime.now(dt.timezone.utc)
    alt = [x509.DNSName("localhost")]
    try:
        alt.append(x509.DNSName(socket.gethostname()))
    except Exception:
        pass
    for ip in set(ips) | {"127.0.0.1"}:
        alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        # Apple accetta al massimo ~398 giorni
        .not_valid_after(now + dt.timedelta(days=390))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _still_valid(cert_path: Path, ips: list[str]) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        covered = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        expires = cert.not_valid_after_utc
    except Exception:
        return False
    if expires - dt.datetime.now(dt.timezone.utc) < dt.timedelta(days=14):
        return False
    return all(ip in covered for ip in ips)
