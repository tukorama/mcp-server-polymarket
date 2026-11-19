"""Facts about Polymarket identifiers that need no signing libraries."""

import re

_V2_RESERVED_BITS = ((1 << 64) - 1) << 40
_UINT = re.compile(r"^[0-9]+$")


def is_v2_token(token_id: str) -> bool:
    """Polymarket Protocol V2 position ids leave bits 40-103 empty; CTF token ids do not."""
    if not _UINT.match(token_id):
        return False
    return (int(token_id) & _V2_RESERVED_BITS) == 0
