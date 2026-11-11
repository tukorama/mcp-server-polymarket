import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from mcp_server_polymarket.client.session import write_private

logger = logging.getLogger(__name__)

MINUTE = 60
HOUR = 3600
DAY = 24 * 3600

_now = time.time


class JsonCache:
    """Memory plus file cache for slow-changing catalogues: documentation indexes, Perps
    instruments, sports metadata, market metadata for confirmations."""

    def __init__(self, directory: Path, *, persist: bool = True) -> None:
        self.directory = directory
        self.persist = persist
        self._memory: dict[str, tuple[float, Any]] = {}

    def _path(self, key: str) -> Path:
        return self.directory / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

    def get(self, key: str, ttl: float) -> Any | None:
        hit = self._memory.get(key)
        if hit and _now() - hit[0] <= ttl:
            return hit[1]
        if not self.persist:
            return None
        try:
            entry = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or _now() - entry.get("saved_at", 0) > ttl:
            return None
        self._memory[key] = (entry["saved_at"], entry.get("data"))
        return entry.get("data")

    def set(self, key: str, data: Any, *, disk: bool = True) -> None:
        saved = _now()
        self._memory[key] = (saved, data)
        if not (self.persist and disk):
            return
        try:
            write_private(self._path(key), {"saved_at": saved, "data": data})
        except OSError as exc:
            logger.warning("Could not write cache entry: %s", exc)
