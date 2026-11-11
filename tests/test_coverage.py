"""Every endpoint Polymarket publishes is called by a tool, or listed with a reason."""

import json
import re
from pathlib import Path

from mcp_server_polymarket import coverage
from mcp_server_polymarket.client import endpoints as ep

MANIFEST = json.loads((Path(__file__).parent / "fixtures" / "api_manifest.json").read_text())


def normalize(key: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", key)


def published() -> set[str]:
    return {normalize(f"{e['service']} {e['method']} {e['path']}") for e in MANIFEST["endpoints"]}


def implemented() -> set[str]:
    return {normalize(e.key) for e in ep.ALL}


def test_every_published_endpoint_is_accounted_for():
    accounted = (
        implemented() | set(coverage.SUPERSEDED) | set(coverage.WAIVED) | set(coverage.PLANNED)
    )
    missing = sorted(published() - accounted)
    assert not missing, "Published endpoints with no tool and no reason:\n" + "\n".join(missing)


def test_every_called_endpoint_is_published_or_documented():
    unknown = sorted(implemented() - published() - set(coverage.EXTRA))
    assert not unknown, "Endpoints missing from the specs and from EXTRA:\n" + "\n".join(unknown)


def test_the_tables_do_not_overlap_or_go_stale():
    tables = [coverage.SUPERSEDED, coverage.WAIVED, coverage.PLANNED]
    keys = [key for table in tables for key in table]
    assert len(keys) == len(set(keys))
    assert not (set(keys) & implemented()), "an endpoint is both called and excused"
    stale = sorted(set(keys) - published())
    assert not stale, "Excused endpoints that are no longer published:\n" + "\n".join(stale)
    assert not (set(coverage.EXTRA) - implemented()), "EXTRA lists endpoints nothing calls"


def test_tool_count_stays_within_provider_limits():
    # Several LLM providers cap a request at 128 tools, and `all` exposes every one.
    from mcp_server_polymarket.capabilities import REQUIREMENTS as needs
    from mcp_server_polymarket.capabilities import Capabilities, parse_toolsets
    from mcp_server_polymarket.ops import OPERATIONS, enabled_operations

    everything = Capabilities(
        toolsets=parse_toolsets("all"),
        have=frozenset(needs),
        gates=frozenset({"cancel", "trading", "transfers"}),
    )
    assert len(enabled_operations(everything)) == len(OPERATIONS) == 128
