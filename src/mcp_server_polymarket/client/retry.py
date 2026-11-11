import random
from dataclasses import dataclass

import httpx2


@dataclass(frozen=True)
class RetryPolicy:
    rate_limit_attempts: int = 5
    server_attempts: int = 3
    network_attempts: int = 3
    retry_ambiguous_timeouts: bool = True


RETRY_SAFE = RetryPolicy()
# A write that may have reached the server must not be repeated: it could place an order or move
# money twice. A 429 or a refused connection is known not to have been processed.
RETRY_UNSAFE = RetryPolicy(server_attempts=1, retry_ambiguous_timeouts=False)


def backoff(attempt: int) -> float:
    return min(2 * 1.6**attempt, 60) + random.uniform(0, 1)


def parse_retry_after(response: httpx2.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))
