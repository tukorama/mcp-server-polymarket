import base64
import re
import unicodedata
from typing import Any

_SECRETS: set[str] = set()

# Header and field names whose values are credentials. Hex ids are not masked by shape, because
# condition ids, order hashes and transaction hashes look exactly like private keys.
_SECRET_NAMES = (
    r"poly_(?:api_key|passphrase|signature|builder_api_key|builder_passphrase|builder_signature)"
    r"|relayer_api_key|polymarket-secret|secret|passphrase|private_?key|api_?secret|authorization"
    r"|proxy_?key|apiKey"
)
_PATTERNS = [
    (
        re.compile(rf"(?i)(['\"]?(?:{_SECRET_NAMES})['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}}]+"),
        r"\1***",
    ),
    (re.compile(r"(?i)(\bbearer\s+)[\w+/=.-]+"), r"\1***"),
]
_CONTROL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f‪-‮⁦-⁩]")


def register_secret(value: str | None) -> None:
    """Remember a literal secret so it is masked wherever it shows up in logs or errors."""
    if value and len(value) >= 6:
        _SECRETS.add(value)
        _SECRETS.add(base64.b64encode(value.encode()).decode())
        if value.startswith("0x"):
            _SECRETS.add(value[2:])


def scrub(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clean_label(text: Any, limit: int = 200) -> str | None:
    """Untrusted text for one line of a confirmation: cleaned, with whitespace folded."""
    if text is None:
        return None
    cleaned = clean_text(str(text), limit)
    return " ".join(cleaned.split()) if cleaned else cleaned


def clean_text(text: str | None, limit: int | None = None) -> str | None:
    """Untrusted text from Polymarket users: drop control and bidi characters, then cap it."""
    if text is None:
        return None
    cleaned = _CONTROL.sub("", unicodedata.normalize("NFC", text)).strip()
    if limit is not None and len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "…"
    return cleaned
