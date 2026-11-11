"""Credentials this server derived or was given, and where they live on disk."""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mcp_server_polymarket.redaction import register_secret


def write_private(path: Path, payload: Any) -> None:
    """Atomically write JSON readable only by the current user."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class ClobCredentials:
    """CLOB L2 API credentials. `address` is the signer they belong to."""

    address: str
    api_key: str
    secret: str = field(repr=False)
    passphrase: str = field(repr=False)

    def register(self) -> None:
        for value in (self.api_key, self.secret, self.passphrase):
            register_secret(value)


@dataclass(frozen=True)
class PerpsSession:
    """A Perps trading key. Without `proxy_key` it can read but not sign."""

    owner: str
    proxy: str
    secret: str = field(repr=False)
    proxy_key: str | None = field(default=None, repr=False)
    expires_at_ms: int | None = None
    label: str | None = None

    @property
    def can_sign(self) -> bool:
        return self.proxy_key is not None

    def register(self) -> None:
        register_secret(self.secret)
        if self.proxy_key:
            register_secret(self.proxy_key)


class CredentialStore:
    """`session.json` keeps CLOB credentials per signer; `perps_session.json` the Perps key.

    Private keys of the user's wallet are never written. The Perps proxy key is written, because
    it is generated here and only ever authorised for trading.
    """

    def __init__(self, session_file: Path, perps_file: Path) -> None:
        self.session_file = session_file
        self.perps_file = perps_file

    def load_clob(self, address: str | None = None) -> ClobCredentials | None:
        data = _read(self.session_file) or {}
        entries = data.get("clob") or {}
        if not isinstance(entries, dict) or not entries:
            return None
        key = address.lower() if address else next(iter(entries))
        raw = entries.get(key)
        if not isinstance(raw, dict):
            return None
        try:
            credentials = ClobCredentials(
                address=key,
                api_key=str(raw["api_key"]),
                secret=str(raw["secret"]),
                passphrase=str(raw["passphrase"]),
            )
        except KeyError:
            return None
        credentials.register()
        return credentials

    def save_clob(self, credentials: ClobCredentials) -> None:
        data = _read(self.session_file) or {}
        current = data.get("clob")
        entries: dict[str, Any] = current if isinstance(current, dict) else {}
        entries[credentials.address.lower()] = {
            "api_key": credentials.api_key,
            "secret": credentials.secret,
            "passphrase": credentials.passphrase,
        }
        data["clob"] = entries
        data["version"] = 1
        write_private(self.session_file, data)

    def forget_clob(self, address: str) -> bool:
        data = _read(self.session_file) or {}
        entries = data.get("clob") or {}
        if address.lower() not in entries:
            return False
        del entries[address.lower()]
        data["clob"] = entries
        write_private(self.session_file, data)
        return True

    def load_perps(self) -> PerpsSession | None:
        raw = _read(self.perps_file)
        if not raw:
            return None
        try:
            session = PerpsSession(
                owner=str(raw["owner"]),
                proxy=str(raw["proxy"]),
                secret=str(raw["secret"]),
                proxy_key=raw.get("proxy_key"),
                expires_at_ms=raw.get("expires_at_ms"),
                label=raw.get("label"),
            )
        except KeyError:
            return None
        session.register()
        return session

    def save_perps(self, session: PerpsSession) -> None:
        write_private(self.perps_file, asdict(session))

    def forget_perps(self) -> bool:
        existed = self.perps_file.exists()
        self.perps_file.unlink(missing_ok=True)
        return existed
