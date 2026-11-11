"""Fetch public, read-only Polymarket endpoints and save each response.

    uv run python scripts/probe.py OUTPUT_DIR

Runs in the Probe workflow. Sends no credentials. The output becomes test fixtures, trimmed by
hand, and `summary.json` shows which hosts answered.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx2

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
RELAYER = "https://relayer-v2.polymarket.com"
BRIDGE = "https://bridge.polymarket.com"
COMBOS = "https://combos-rfq-api.polymarket.com"
PERPS = "https://api.perpetuals.polymarket.com"
WEB = "https://polymarket.com"
# A public example wallet from the Data API documentation.
WALLET = "0x983eedfbd75803602e4a6e6ea9aab6dc6b9c6748"


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}
    client = httpx2.Client(timeout=30, headers={"User-Agent": "mcp-server-polymarket-probe"})

    def get(name: str, method: str, url: str, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            response = client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            summary[name] = {"error": type(exc).__name__}
            return None
        entry: dict[str, Any] = {
            "status": response.status_code,
            "ms": int((time.monotonic() - started) * 1000),
            "bytes": len(response.content),
        }
        summary[name] = entry
        try:
            body = response.json()
        except ValueError:
            body = response.text[:2000]
        (out / f"{name}.json").write_text(json.dumps(body, indent=1)[:400_000])
        return body if response.status_code < 400 else None

    markets = get(
        "gamma_markets_keyset",
        "GET",
        f"{GAMMA}/markets/keyset",
        params={"limit": 3, "closed": "false", "order": "volume24hr", "ascending": "false"},
    )
    market = (markets or {}).get("markets", [{}])[0] if isinstance(markets, dict) else {}
    condition = market.get("conditionId")
    tokens = json.loads(market.get("clobTokenIds") or "[]")
    token = tokens[0] if tokens else None
    events = get(
        "gamma_events_keyset",
        "GET",
        f"{GAMMA}/events/keyset",
        params={"limit": 2, "closed": "false"},
    )
    event = (events or {}).get("events", [{}])[0] if isinstance(events, dict) else {}
    get(
        "gamma_search",
        "GET",
        f"{GAMMA}/public-search",
        params={"q": "bitcoin", "limit_per_type": 2},
    )
    get("gamma_profile", "GET", f"{GAMMA}/public-profile", params={"address": WALLET})

    get("clob_time", "GET", f"{CLOB}/time")
    if token:
        get("clob_book", "GET", f"{CLOB}/book", params={"token_id": token})
        get("clob_books", "POST", f"{CLOB}/books", json=[{"token_id": t} for t in tokens[:2]])
        get("clob_market_by_token", "GET", f"{CLOB}/markets-by-token/{token}")
        get(
            "clob_prices_history",
            "GET",
            f"{CLOB}/prices-history",
            params={"market": token, "interval": "1d", "fidelity": 60},
        )
    if condition:
        get("clob_market_info", "GET", f"{CLOB}/clob-markets/{condition}")
        get("clob_live_activity", "GET", f"{CLOB}/markets/live-activity/{condition}")
        get("clob_rewards_market", "GET", f"{CLOB}/rewards/markets/{condition}")
    get("clob_simplified_markets", "GET", f"{CLOB}/sampling-simplified-markets")
    get("clob_rewards_current", "GET", f"{CLOB}/rewards/markets/current")
    get("clob_rewards_multi", "GET", f"{CLOB}/rewards/markets/multi", params={"page_size": 2})

    get("data_status", "GET", f"{DATA}/v2/status")
    get("data_positions", "GET", f"{DATA}/v2/positions", params={"user": WALLET, "limit": 3})
    get("data_value", "GET", f"{DATA}/v2/value", params={"user": WALLET})
    get("data_user_stats", "GET", f"{DATA}/v2/user-stats", params={"user": WALLET})
    get("data_user_volume", "GET", f"{DATA}/v2/user-volume", params={"user": WALLET})
    get("data_user_pnl", "GET", f"{DATA}/v2/user-pnl", params={"user": WALLET, "interval": "1m"})
    get("data_trades", "GET", f"{DATA}/v2/trades", params={"limit": 3})
    get("data_activity", "GET", f"{DATA}/v2/activity", params={"user": WALLET, "limit": 3})
    get("data_approvals", "GET", f"{DATA}/v2/approvals", params={"user": WALLET})
    get("data_leaderboard", "GET", f"{DATA}/v2/leaderboard", params={"limit": 3})
    get("data_biggest_winners", "GET", f"{DATA}/v2/biggest-winners", params={"limit": 3})
    get("data_builders", "GET", f"{DATA}/v2/builders/leaderboard", params={"limit": 3})
    get("data_builder_volume", "GET", f"{DATA}/v2/builders/volume", params={"limit": 3})
    if condition:
        get(
            "data_holders", "GET", f"{DATA}/v2/holders", params={"condition": condition, "limit": 3}
        )
        get("data_oi", "GET", f"{DATA}/v2/oi", params={"condition": condition})
        get("data_resolutions", "GET", f"{DATA}/v2/resolutions", params={"condition": condition})
        get("data_tokens", "GET", f"{DATA}/v2/tokens", params={"condition": condition})
    if token:
        get(
            "data_prices_history",
            "GET",
            f"{DATA}/v2/prices-history",
            params={"token_id": token, "interval": "1w"},
        )
    if event.get("id"):
        get("data_live_volume", "GET", f"{DATA}/v2/live-volume", params={"event_id": event["id"]})

    get("bridge_assets", "GET", f"{BRIDGE}/supported-assets")
    get("bridge_status", "GET", f"{BRIDGE}/status/{WALLET}", params={"limit": 2})
    get(
        "relayer_deployed", "GET", f"{RELAYER}/deployed", params={"address": WALLET, "type": "SAFE"}
    )
    get("combo_markets", "GET", f"{COMBOS}/v1/rfq/combo-markets", params={"limit": 3})
    get("web_geoblock", "GET", f"{WEB}/api/geoblock")
    get("perps_instruments", "GET", f"{PERPS}/v1/info/instruments")
    get("perps_tickers", "GET", f"{PERPS}/v1/info/tickers", params={"instrument_id": 1})

    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "probe-output")))
