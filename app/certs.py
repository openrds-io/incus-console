"""The client certificate this install presents to remote Incus servers."""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def generate_self_signed(
    common_name: str, crt_path: Path, key_path: Path, *, server: bool = False
) -> None:
    key = ec.generate_private_key(ec.SECP384R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    usage = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .sign(key, hashes.SHA384())
    )
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_client_cert(data_dir: str | Path) -> tuple[str, str]:
    """Return (certificate, key) paths, creating the pair on first use."""
    data_dir = Path(data_dir)
    crt, key = data_dir / "client.crt", data_dir / "client.key"
    if not (crt.exists() and key.exists()):
        generate_self_signed("incus-console", crt, key)
    return str(crt), str(key)


def fingerprint(crt_path: str | Path) -> str:
    cert = x509.load_pem_x509_certificate(Path(crt_path).read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
