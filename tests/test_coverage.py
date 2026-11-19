"""Every endpoint Polymarket publishes is called by a tool, or listed with a reason."""

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from mcp_server_polymarket import coverage
from mcp_server_polymarket.capabilities import REQUIREMENTS as needs
from mcp_server_polymarket.capabilities import Capabilities, parse_toolsets
from mcp_server_polymarket.client import endpoints as ep
from mcp_server_polymarket.ops import OPERATIONS, enabled_operations

MANIFEST = json.loads(
    (Path(__file__).parent / "fixtures" / "api_manifest.json").read_text(encoding="utf-8")
)


def normalize(key: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", key)


def published() -> set[str]:
    return {normalize(f"{e['service']} {e['method']} {e['path']}") for e in MANIFEST["endpoints"]}


def implemented() -> set[str]:
    return {normalize(e.key) for e in ep.ALL}


def test_every_published_endpoint_is_accounted_for() -> None:
    accounted = (
        implemented() | set(coverage.SUPERSEDED) | set(coverage.WAIVED) | set(coverage.PLANNED)
    )
    missing = sorted(published() - accounted)
    assert not missing, "Published endpoints with no tool and no reason:\n" + "\n".join(missing)


def test_every_called_endpoint_is_published_or_documented() -> None:
    unknown = sorted(implemented() - published() - set(coverage.EXTRA))
    assert not unknown, "Endpoints missing from the specs and from EXTRA:\n" + "\n".join(unknown)


def test_the_tables_do_not_overlap_or_go_stale() -> None:
    tables = [coverage.SUPERSEDED, coverage.WAIVED, coverage.PLANNED]
    keys = [key for table in tables for key in table]
    assert len(keys) == len(set(keys))
    assert not (set(keys) & implemented()), "an endpoint is both called and excused"
    stale = sorted(set(keys) - published())
    assert not stale, "Excused endpoints that are no longer published:\n" + "\n".join(stale)
    assert not (set(coverage.EXTRA) - implemented()), "EXTRA lists endpoints nothing calls"


def test_tool_count_stays_within_provider_limits() -> None:
    # Several LLM providers cap a request at 128 tools, and `all` exposes every one.
    everything = Capabilities(
        toolsets=parse_toolsets("all"),
        have=frozenset(needs),
        gates=frozenset({"cancel", "trading", "transfers"}),
    )
    assert len(enabled_operations(everything)) == len(OPERATIONS) == 128


_LAYER_BY_MODULE: dict[str, int] = {
    "__init__": 0,
    "errors": 0,
    "protocol": 0,
    "redaction": 0,
    "logging_setup": 0,
    "coverage": 0,
    "knowledge": 0,
    "config": 1,
    "journal": 1,
    "models": 1,
    "signing": 1,
    "client": 2,
    "identity": 2,
    "capabilities": 2,
    "wallet_tx": 2,
    "auth_flow": 2,
    "safety": 2,
    "ops": 3,
    "prompts": 4,
    "resources": 4,
    "http_app": 4,
    "tools": 5,
    "server": 5,
    "cli": 5,
    "integrations": 5,
    "__main__": 5,
}


def _top_module(path: Path, pkg_root: Path) -> str:
    rel = path.relative_to(pkg_root)
    return rel.stem if len(rel.parts) == 1 else rel.parts[0]


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _collect_runtime_imports(node: ast.AST) -> set[str]:
    targets: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and _is_type_checking_guard(child.test):
            for else_stmt in child.orelse:
                targets |= _collect_runtime_imports(else_stmt)
            continue
        if isinstance(child, ast.Import):
            for alias in child.names:
                parts = alias.name.split(".")
                if parts[0] == "mcp_server_polymarket" and len(parts) > 1:
                    targets.add(parts[1])
        elif isinstance(child, ast.ImportFrom) and child.module:
            if child.module == "mcp_server_polymarket":
                for alias in child.names:
                    if alias.name != "__version__":
                        targets.add(alias.name)
            elif child.module.startswith("mcp_server_polymarket."):
                targets.add(child.module.split(".")[1])
        else:
            targets |= _collect_runtime_imports(child)
    return targets


def test_strict_architectural_layer_dag() -> None:
    """Every runtime import inside the package must stay on the same or a lower layer."""
    pkg_root = Path(__file__).parent.parent / "src" / "mcp_server_polymarket"
    py_files = sorted(pkg_root.rglob("*.py"))
    assert py_files
    seen_modules = {_top_module(path, pkg_root) for path in py_files}
    assert seen_modules <= set(_LAYER_BY_MODULE), (
        f"Unmapped package modules: {sorted(seen_modules - set(_LAYER_BY_MODULE))}"
    )
    violations: list[str] = []
    for path in py_files:
        src_mod = _top_module(path, pkg_root)
        src_layer = _LAYER_BY_MODULE[src_mod]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for tgt_mod in sorted(_collect_runtime_imports(tree)):
            assert tgt_mod in _LAYER_BY_MODULE, f"{path}: unknown internal import {tgt_mod!r}"
            tgt_layer = _LAYER_BY_MODULE[tgt_mod]
            if tgt_layer > src_layer:
                violations.append(
                    f"{path.relative_to(pkg_root)} (L{src_layer}:{src_mod}) -> "
                    + f"{tgt_mod} (L{tgt_layer})"
                )
    assert not violations, "Upward runtime imports found:\n" + "\n".join(violations)


def test_gitleaks_config_and_custom_patterns_pass_repo_files() -> None:
    """Custom Gitleaks patterns must be valid and find zero secrets outside test fixtures."""
    root = Path(__file__).parent.parent
    config = tomllib.loads((root / ".gitleaks.toml").read_text(encoding="utf-8"))
    rules: list[dict[str, Any]] = list(config.get("rules", []))
    rule_ids = {rule["id"] for rule in rules}
    assert {
        "polymarket-evm-private-key",
        "polymarket-ed25519-key",
        "polymarket-hmac-secret",
    } <= rule_ids

    scan_files: list[Path] = [
        *sorted((root / "src").rglob("*.py")),
        root / "README.md",
        root / "pyproject.toml",
        root / "server.json",
    ]
    onboarding_dir = root / "onboarding"
    if onboarding_dir.exists():
        scan_files.extend(sorted(onboarding_dir.rglob("*.md")))

    hits: list[str] = []
    for rule in rules:
        pattern = re.compile(rule["regex"], re.MULTILINE)
        for path in scan_files:
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                hits.append(f"{rule['id']} matched {path.relative_to(root)}")
    assert not hits, "Secret patterns matched repository files:\n" + "\n".join(hits)
