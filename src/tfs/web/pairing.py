"""One-time pairing links and the phones that redeemed them."""

import hashlib
import hmac
import secrets
import socket
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tfs import config
from tfs.connectors.core.contract import to_utc_text
from tfs.persistence.migrations import migrate
from tfs.persistence.sqlite import connect, transaction


def lan_address() -> str:
    """Return the IPv4 address the computer uses on its local network."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
        except OSError:
            return socket.gethostbyname(socket.gethostname())
        return probe.getsockname()[0]


def wifi_host() -> str:
    """Return the host used by both pairing links and push URLs."""
    return f"{socket.gethostname()}.local" if sys.platform == "darwin" else lan_address()


def _hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def make_pairing_link(*, port: int | None = None) -> str:
    """Create a link that one phone can redeem in the next ten minutes."""
    code = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    config.data_dir().mkdir(parents=True, exist_ok=True)
    with closing(connect(config.db_path())) as conn:
        migrate(conn)
        with transaction(conn):
            conn.execute(
                "DELETE FROM phone_pairings WHERE code_expires_at <= ?", (to_utc_text(now),)
            )
            conn.execute(
                "INSERT INTO phone_pairings VALUES (?, ?)",
                (_hash(code), to_utc_text(now + timedelta(minutes=10))),
            )
    return f"http://{wifi_host()}:{port or config.HOME_WIFI_PORT}/pair/{code}"


def redeem(db: Path, code: str) -> str | None:
    """Replace a fresh code with a phone token in one transaction."""
    digest = _hash(code)
    with closing(connect(db)) as conn, transaction(conn):
        row = conn.execute(
            "SELECT secret_hash FROM phone_pairings WHERE secret_hash = ? AND code_expires_at > ?",
            (digest, to_utc_text(datetime.now(UTC))),
        ).fetchone()
        if row is None or not hmac.compare_digest(row[0], digest):
            return None
        token = secrets.token_urlsafe(32)
        conn.execute(
            "UPDATE phone_pairings SET secret_hash = ?, code_expires_at = NULL WHERE secret_hash = ?",
            (_hash(token), digest),
        )
    return token


def paired(db: Path, token: str) -> bool:
    """Check that this token belongs to a phone that is still paired."""
    digest = _hash(token)
    with closing(connect(db, "read_only")) as conn:
        row = conn.execute(
            "SELECT secret_hash FROM phone_pairings WHERE secret_hash = ? AND code_expires_at IS NULL",
            (digest,),
        ).fetchone()
    return row is not None and hmac.compare_digest(row[0], digest)
