"""TLS certs for the fake Juju controller.

Generates a fresh self-signed CA + server cert via `trustme`. Each test
session gets a unique CA so a stale cert in the repo can't bypass
verification. The files land in a pytest tmp dir; the ``juju_ops_env``
fixture points ``JUJU_CACERT`` at the CA.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import trustme


@dataclass
class FakeCerts:
    """Paths to the generated cert / key / CA files."""

    cert_path: Path
    key_path: Path
    ca_path: Path


def generate_certs(out_dir: Path) -> FakeCerts:
    """Generate a fresh CA + server cert for localhost into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ca = trustme.CA()
    server_cert = ca.issue_cert("localhost", "127.0.0.1")

    certs = FakeCerts(
        cert_path=out_dir / "server.pem",
        key_path=out_dir / "server.key",
        ca_path=out_dir / "ca.pem",
    )
    ca.cert_pem.write_to_path(str(certs.ca_path))
    server_cert.cert_chain_pems[0].write_to_path(str(certs.cert_path))
    server_cert.private_key_pem.write_to_path(str(certs.key_path))
    return certs


__all__ = ["FakeCerts", "generate_certs"]
