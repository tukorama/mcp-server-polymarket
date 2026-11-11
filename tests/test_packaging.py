import email
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_wheel_contents_and_metadata(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--no-sources", "--out-dir", str(tmp_path)],
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        entry_points = archive.read(metadata_name.replace("METADATA", "entry_points.txt")).decode()
    expected = ["mcp_server_polymarket/py.typed"]
    expected += [
        f"mcp_server_polymarket/knowledge/{topic}.md"
        for topic in ("concepts", "trading", "perps", "safety")
    ]
    for name in expected:
        assert name in names
    assert any(name.endswith("licenses/NOTICE") for name in names)
    assert not [name for name in names if name.startswith("tests/") or "__pycache__" in name]
    assert metadata["License-Expression"] == "MIT"
    assert "mcp-server-polymarket = mcp_server_polymarket.cli:main" in entry_points
    assert "signing" in metadata.get_all("Provides-Extra")

    # The MCP Registry checks PyPI for this line, and a release cannot be edited afterwards.
    description = metadata.get_payload()
    manifest = json.loads((ROOT / "server.json").read_text())
    assert f"mcp-name: {manifest['name']}" in description
    # PyPI does not rewrite relative links, so they would break on the project page.
    assert not re.findall(r"\]\((?!https?://|#)", description)


def test_versions_agree():
    pyproject = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    manifest = json.loads((ROOT / "server.json").read_text())
    assert manifest["version"] == version
    assert {package["version"] for package in manifest["packages"]} == {version}
