import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict

from mcp_server_polymarket.errors import ConfigError
from mcp_server_polymarket.redaction import register_secret

ADDRESS = re.compile(r"0x[0-9a-f]{40}")

APP_DIR = "mcp-server-polymarket"

# Each service's base URL. Overriding one is for testing against a mirror; credentials are sent
# to these hosts, so they must use https.
SERVICE_URLS = {
    "gamma_url": "https://gamma-api.polymarket.com",
    "clob_url": "https://clob.polymarket.com",
    "data_url": "https://data-api.polymarket.com",
    "relayer_url": "https://relayer-v2.polymarket.com",
    "bridge_url": "https://bridge.polymarket.com",
    "combos_url": "https://combos-rfq-api.polymarket.com",
    "combos_requester_url": "https://combos-rfq-gateway-requester-api.polymarket.com",
    "collateral_return_url": "https://combos-rfq-collateral-return.polymarket.com",
    "perps_url": "https://api.perpetuals.polymarket.com",
    "web_url": "https://polymarket.com",
    "docs_url": "https://docs.polymarket.com",
    "clob_ws_url": "wss://ws-subscriptions-clob.polymarket.com",
    "sports_ws_url": "wss://sports-api.polymarket.com/ws",
    "live_ws_url": "wss://ws-live-v2.polymarket.com/ws",
    "perps_ws_url": "wss://ws.perpetuals.polymarket.com/v1/ws",
}

# A .env file comes along with whatever directory the client happens to start in, so it must not
# be able to redirect credentials, switch on money-moving tools or loosen the limits.
_NOT_FROM_DOTENV = {
    *SERVICE_URLS,
    "private_key",
    "private_key_file",
    "wallet_address",
    "signature_type",
    "signer_role",
    "api_key",
    "api_secret",
    "api_passphrase",
    "address",
    "relayer_api_key",
    "relayer_api_key_address",
    "perps_proxy",
    "perps_secret",
    "perps_proxy_key",
    "allow_cancel",
    "allow_trading",
    "allow_transfers",
    "confirm",
    "max_order_usd",
    "max_daily_usd",
    "max_perps_order_usd",
    "max_perps_leverage",
    "max_transfer_usd",
    "max_price_deviation",
    "transfer_allowlist",
    "builder_api_key",
    "builder_secret",
    "builder_passphrase",
    "builder_code",
    "perps_builder",
    "session_file",
    "perps_session_file",
    "journal_file",
    "cache_dir",
    "http_bearer_token",
}

Limit = Decimal | Literal["unlimited"]


def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR


def cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_DIR


class _RestrictedDotEnv(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], source: PydanticBaseSettingsSource):
        super().__init__(settings_cls)
        self.source = source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self.source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        return {key: val for key, val in self.source().items() if key not in _NOT_FROM_DOTENV}


def _limit(value: Any) -> Any:
    # Only the literal word lifts a limit: "off" or "none" could as well mean "switched off".
    if isinstance(value, str) and value.strip().lower() == "unlimited":
        return "unlimited"
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("expected a number, 0 to block, or 'unlimited'") from exc
    if not number.is_finite():
        raise ValueError("expected a number, 0 to block, or 'unlimited'")
    if number < 0:
        raise ValueError("must not be negative")
    return number


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLYMARKET_",
        env_file=".env",
        extra="ignore",
        env_ignore_empty=True,
        validate_assignment=True,
    )

    # Signing identity. Without a key the server only reads.
    private_key: SecretStr | None = None
    private_key_file: Path | None = None
    wallet_address: str | None = None
    signature_type: int | None = Field(default=None, ge=0, le=3)
    signer_role: Literal["owner", "session_key"] = "owner"

    # Ready-made CLOB API credentials, for account reads without a private key.
    api_key: str | None = None
    api_secret: SecretStr | None = None
    api_passphrase: SecretStr | None = None
    address: str | None = None

    relayer_api_key: SecretStr | None = None
    relayer_api_key_address: str | None = None

    builder_api_key: str | None = None
    builder_secret: SecretStr | None = None
    builder_passphrase: SecretStr | None = None
    builder_code: str | None = None
    perps_builder: str | None = None

    perps_proxy: str | None = None
    perps_secret: SecretStr | None = None
    perps_proxy_key: SecretStr | None = None

    # Gates and limits for anything that moves money.
    allow_cancel: bool = False
    allow_trading: bool = False
    allow_transfers: bool = False
    confirm: Literal["elicit", "none"] = "elicit"
    max_order_usd: Limit = Decimal(100)
    max_daily_usd: Limit = Decimal(1000)
    max_perps_order_usd: Limit = Decimal(500)
    max_perps_leverage: Limit = Decimal(5)
    max_transfer_usd: Limit = Decimal(100)
    max_price_deviation: Limit = Decimal("0.10")
    # Comma-separated in the environment; NoDecode keeps pydantic-settings from reading JSON.
    transfer_allowlist: Annotated[list[str], NoDecode] = []

    toolsets: str = "auto"

    gamma_url: str = SERVICE_URLS["gamma_url"]
    clob_url: str = SERVICE_URLS["clob_url"]
    data_url: str = SERVICE_URLS["data_url"]
    relayer_url: str = SERVICE_URLS["relayer_url"]
    bridge_url: str = SERVICE_URLS["bridge_url"]
    combos_url: str = SERVICE_URLS["combos_url"]
    combos_requester_url: str = SERVICE_URLS["combos_requester_url"]
    collateral_return_url: str = SERVICE_URLS["collateral_return_url"]
    perps_url: str = SERVICE_URLS["perps_url"]
    web_url: str = SERVICE_URLS["web_url"]
    docs_url: str = SERVICE_URLS["docs_url"]
    clob_ws_url: str = SERVICE_URLS["clob_ws_url"]
    sports_ws_url: str = SERVICE_URLS["sports_ws_url"]
    live_ws_url: str = SERVICE_URLS["live_ws_url"]
    perps_ws_url: str = SERVICE_URLS["perps_ws_url"]

    session_file: Path | None = None
    perps_session_file: Path | None = None
    journal_file: Path | None = None
    cache_dir: Path | None = None
    http_timeout: float = Field(default=20, gt=0)
    http_bearer_token: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _RestrictedDotEnv(settings_cls, dotenv_settings),
            file_secret_settings,
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "max_order_usd",
        "max_daily_usd",
        "max_perps_order_usd",
        "max_perps_leverage",
        "max_transfer_usd",
        "max_price_deviation",
        mode="before",
    )
    @classmethod
    def _parse_limit(cls, value: Any) -> Any:
        return _limit(value)

    @field_validator("transfer_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, value: Any) -> Any:
        parts = value.split(",") if isinstance(value, str) else value
        cleaned = [str(part).strip().lower() for part in parts if str(part).strip()]
        for address in cleaned:
            if not ADDRESS.fullmatch(address):
                raise ValueError(f"POLYMARKET_TRANSFER_ALLOWLIST has an invalid address: {address}")
        return cleaned

    @field_validator(*SERVICE_URLS, mode="after")
    @classmethod
    def _url_is_safe(cls, value: str) -> str:
        parts = urlsplit(value)
        local = parts.hostname in ("127.0.0.1", "localhost", "::1")
        if parts.scheme not in ("https", "wss") and not local:
            raise ValueError("must use https or wss, since credentials are sent to it")
        return value.rstrip("/")

    # Paths

    @property
    def resolved_session_file(self) -> Path:
        return (self.session_file or config_dir() / "session.json").expanduser()

    @property
    def resolved_perps_session_file(self) -> Path:
        return (self.perps_session_file or config_dir() / "perps_session.json").expanduser()

    @property
    def resolved_journal_file(self) -> Path:
        return (self.journal_file or config_dir() / "journal.sqlite3").expanduser()

    @property
    def resolved_cache_dir(self) -> Path:
        return (self.cache_dir or cache_dir()).expanduser()

    # Secrets

    def signer_key(self) -> str | None:
        """The private key that signs, from the environment or a key file."""
        if self.private_key is not None:
            key = self.private_key.get_secret_value().strip()
        elif self.private_key_file is not None:
            path = self.private_key_file.expanduser()
            try:
                key = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(
                    f"POLYMARKET_PRIVATE_KEY_FILE could not be read: {type(exc).__name__}"
                ) from exc
        else:
            return None
        if not key.startswith("0x"):
            key = f"0x{key}"
        if len(key) != 66:
            raise ConfigError("The private key must be 32 bytes of hex.")
        register_secret(key)
        return key

    def register_secrets(self) -> None:
        for value in (
            self.api_secret,
            self.api_passphrase,
            self.relayer_api_key,
            self.builder_secret,
            self.builder_passphrase,
            self.perps_secret,
            self.perps_proxy_key,
            self.private_key,
            self.http_bearer_token,
        ):
            if value is not None:
                register_secret(value.get_secret_value())
        if self.api_key:
            register_secret(self.api_key)
        if self.builder_api_key:
            register_secret(self.builder_api_key)

    def url(self, name: str) -> str:
        return getattr(self, name)
