"""Who this server acts as: signer, wallet, and the credentials found for them."""

from dataclasses import dataclass, field
from functools import cached_property

from mcp_server_polymarket.client.auth import HmacKey
from mcp_server_polymarket.client.session import ClobCredentials, CredentialStore, PerpsSession
from mcp_server_polymarket.config import Settings
from mcp_server_polymarket.errors import ConfigError


def signing_available() -> bool:
    try:
        import eth_account  # noqa: F401
    except ImportError:
        return False
    return True


def address_of(private_key: str) -> str:
    from eth_account import Account

    return Account.from_key(private_key).address


def checksum(address: str) -> str:
    """EIP-55 form when the signing extra is installed, else the input lower-cased."""
    if not (address.startswith("0x") and len(address) == 42):
        raise ConfigError(f"{address!r} is not an EVM address.")
    try:
        from eth_utils.address import to_checksum_address
    except ImportError:
        return address.lower()
    return to_checksum_address(address)


@dataclass
class Identity:
    settings: Settings
    store: CredentialStore
    notes: list[str] = field(default_factory=list)

    @cached_property
    def signer_key(self) -> str | None:
        return self.settings.signer_key()

    @cached_property
    def signer_address(self) -> str | None:
        key = self.signer_key
        if key is None:
            return None
        if not signing_available():
            self.notes.append(
                "A private key is configured but the `signing` extra is not installed."
            )
            return None
        return address_of(key)

    @cached_property
    def clob_address(self) -> str | None:
        """The address CLOB credentials belong to (POLY_ADDRESS)."""
        if self.settings.address:
            return checksum(self.settings.address)
        return self.signer_address

    def clob_credentials(self) -> ClobCredentials | None:
        settings = self.settings
        if settings.api_key and settings.api_secret and settings.api_passphrase:
            if not self.clob_address:
                raise ConfigError(
                    "POLYMARKET_API_KEY is set without the address it belongs to.",
                    hint="Set POLYMARKET_ADDRESS to the signer address of these credentials.",
                )
            credentials = ClobCredentials(
                address=self.clob_address,
                api_key=settings.api_key,
                secret=settings.api_secret.get_secret_value(),
                passphrase=settings.api_passphrase.get_secret_value(),
            )
            credentials.register()
            return credentials
        if self.clob_address is None:
            return None
        stored = self.store.load_clob(self.clob_address)
        return (
            ClobCredentials(
                address=self.clob_address,
                api_key=stored.api_key,
                secret=stored.secret,
                passphrase=stored.passphrase,
            )
            if stored
            else None
        )

    def relayer_key(self) -> tuple[str, str] | None:
        settings = self.settings
        if settings.relayer_api_key and settings.relayer_api_key_address:
            return settings.relayer_api_key.get_secret_value(), settings.relayer_api_key_address
        return None

    def builder_key(self) -> HmacKey | None:
        settings = self.settings
        if settings.builder_api_key and settings.builder_secret and settings.builder_passphrase:
            return HmacKey(
                settings.builder_api_key,
                settings.builder_secret.get_secret_value(),
                settings.builder_passphrase.get_secret_value(),
            )
        return None

    def perps_session(self) -> PerpsSession | None:
        settings = self.settings
        if settings.perps_proxy and settings.perps_secret:
            session = PerpsSession(
                owner=self.signer_address or "",
                proxy=settings.perps_proxy,
                secret=settings.perps_secret.get_secret_value(),
                proxy_key=settings.perps_proxy_key.get_secret_value()
                if settings.perps_proxy_key
                else None,
            )
            session.register()
            return session
        return self.store.load_perps()

    @property
    def signature_type(self) -> int | None:
        """The wallet's signature type, as account reads and orders need it."""
        return self.settings.signature_type

    @cached_property
    def wallet_address(self) -> str | None:
        if self.settings.wallet_address:
            return checksum(self.settings.wallet_address)
        return None
