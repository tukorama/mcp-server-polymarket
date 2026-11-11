"""Rebuild tests/fixtures/api_manifest.json from the specifications Polymarket publishes.

    uv run --with pyyaml python scripts/sync_api_manifest.py           # fetch and write
    uv run --with pyyaml python scripts/sync_api_manifest.py --check   # fail if it changed
    uv run --with pyyaml python scripts/sync_api_manifest.py --from DIR  # use downloaded specs

The manifest holds only method, path, query parameter names and AsyncAPI operation names, which
is what the coverage test compares with the endpoints this package calls.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

DOCS = "https://docs.polymarket.com"
ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tests" / "fixtures" / "api_manifest.json"
HOSTS = {
    "gamma-api.polymarket.com": "GAMMA",
    "clob.polymarket.com": "CLOB",
    "data-api.polymarket.com": "DATA",
    "relayer-v2.polymarket.com": "RELAYER",
    "bridge.polymarket.com": "BRIDGE",
    "combos-rfq-api.polymarket.com": "COMBOS",
    "api.perpetuals.polymarket.com": "PERPS",
}
# Data API v2 endpoint pages embed the OpenAPI fragment, for when the spec host is unreachable.
DATA_PAGES = [
    "service/get-data-freshness",
    "markets/list-a-markets-top-holders",
    "markets/get-open-interest",
    "markets/get-live-volume-for-an-event",
    "markets/get-resolution-state",
    "markets/look-up-tokens",
    "markets/get-a-tokens-price-history",
    "feeds/list-trades",
    "feeds/list-account-activity",
    "feeds/list-combo-activity",
    "wallet/get-a-users-profile-stats",
    "wallet/list-positions-for-a-user-or-market",
    "wallet/get-portfolio-value",
    "wallet/get-a-users-pnl-series",
    "wallet/get-a-users-trading-volume",
    "wallet/get-wallet-approvals",
    "wallet/list-combo-positions",
    "boards/get-the-trader-leaderboard",
    "boards/list-the-biggest-wins",
    "boards/get-the-builders-leaderboard",
    "boards/get-builder-volume-over-time",
]
_SECTION = re.compile(r"^## (OpenAPI|AsyncAPI) Specs\s*$")
_LINK = re.compile(r"^- \[[^\]]+\]\(([^)]+)\)")


def spec_links(index: str) -> tuple[list[str], list[str]]:
    openapi, asyncapi, current = [], [], None
    for line in index.splitlines():
        heading = _SECTION.match(line)
        if heading:
            current = heading.group(1)
            continue
        if line.startswith("## "):
            current = None
        match = _LINK.match(line)
        if match and current:
            url = urljoin(DOCS + "/", match.group(1).replace("https:/d", "https://d"))
            (openapi if current == "OpenAPI" else asyncapi).append(url)
    return list(dict.fromkeys(openapi)), list(dict.fromkeys(asyncapi))


def load(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        import yaml  # only needed for the YAML specs

        return yaml.safe_load(text)


def openapi_endpoints(spec: dict[str, Any], source: str) -> list[dict[str, Any]]:
    servers = spec.get("servers") or [{}]
    host = urlsplit(servers[0].get("url", "")).hostname or ""
    service = HOSTS.get(host)
    if service is None:
        return []
    rows = []
    for path, operations in (spec.get("paths") or {}).items():
        for method, operation in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            params = []
            for param in operation.get("parameters") or []:
                if "$ref" in param:
                    ref = param["$ref"].lstrip("#/").split("/")
                    param = spec
                    for part in ref:
                        param = param.get(part, {})
                if param.get("in") == "query":
                    params.append(param.get("name"))
            rows.append(
                {
                    "service": service,
                    "method": method.upper(),
                    "path": path,
                    "query": sorted(p for p in params if p),
                    "source": source,
                }
            )
    return rows


def asyncapi_channels(spec: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """One row per AsyncAPI operation: what a client sends or receives on that socket."""
    return [
        {"source": source, "operation": name, "action": (operation or {}).get("action")}
        for name, operation in (spec.get("operations") or {}).items()
    ]


def fetch(client: httpx2.Client, url: str, local: Path | None) -> str | None:
    if local is not None:
        candidate = local / Path(urlsplit(url).path).name
        return candidate.read_text() if candidate.exists() else None
    try:
        response = client.get(url)
    except httpx2.HTTPError:
        return None
    return response.text if response.status_code == 200 else None


def build(local: Path | None) -> dict[str, Any]:
    client = httpx2.Client(timeout=60, headers={"User-Agent": "mcp-server-polymarket-sync"})
    index = fetch(client, f"{DOCS}/llms.txt", None if local is None else local)
    if index is None:
        raise SystemExit("could not read llms.txt")
    openapi, asyncapi = spec_links(index)
    endpoints: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []
    sources = []
    for url in openapi:
        text = fetch(client, url, local)
        source = Path(urlsplit(url).path).name
        if text is None and "data-api" in url:
            endpoints += data_api_from_pages(client, local)
            sources.append(f"{source} (from documentation pages)")
            continue
        if text is None:
            raise SystemExit(f"could not read {url}")
        endpoints += openapi_endpoints(load(text), source)
        sources.append(source)
    for url in asyncapi:
        text = fetch(client, url, local)
        if text is None:
            continue
        try:
            channels += asyncapi_channels(load(text), Path(urlsplit(url).path).name)
        except Exception:
            continue
    unique = {(e["service"], e["method"], e["path"]): e for e in endpoints}
    return {
        "sources": sorted(set(sources)),
        "endpoints": sorted(unique.values(), key=lambda e: (e["service"], e["path"], e["method"])),
        "operations": sorted(
            {(c["source"], c["operation"]): c for c in channels}.values(),
            key=lambda c: (c["source"], c["operation"]),
        ),
    }


def data_api_from_pages(client: httpx2.Client, local: Path | None) -> list[dict[str, Any]]:
    rows = []
    for page in DATA_PAGES:
        name = page.replace("/", "_") + ".md"
        if local is not None:
            path = local / name
            text = path.read_text() if path.exists() else None
        else:
            text = fetch(client, f"{DOCS}/api-reference/{page}.md", None)
        if not text:
            raise SystemExit(f"could not read Data API page {page}")
        match = re.search(r"````yaml[^\n]*\n(.*?)\n````", text, re.S)
        if match:
            spec = load(match.group(1))
            spec["servers"] = [{"url": "https://data-api.polymarket.com"}]
            rows += openapi_endpoints(spec, "data-api-v2 (documentation pages)")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--from", dest="local", type=Path)
    args = parser.parse_args()
    manifest = build(args.local)
    text = json.dumps(manifest, indent=1) + "\n"
    if args.check:
        current = MANIFEST.read_text() if MANIFEST.exists() else ""
        old = json.loads(current) if current else {"endpoints": []}
        before = {(e["service"], e["method"], e["path"]) for e in old["endpoints"]}
        after = {(e["service"], e["method"], e["path"]) for e in manifest["endpoints"]}
        for added in sorted(after - before):
            print("new endpoint:", *added)
        for removed in sorted(before - after):
            print("removed endpoint:", *removed)
        return 1 if before != after else 0
    MANIFEST.write_text(text)
    print(f"{len(manifest['endpoints'])} endpoints, {len(manifest['operations'])} stream operations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
