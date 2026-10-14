"""The relay container example (GW-RELAY-003): what it publishes and what it builds from."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "examples" / "docker"


def test_compose_publishes_only_the_device_port():
    text = (DOCKER / "compose.yaml").read_text()
    assert re.findall(r'^\s+- "(\d+):(\d+)"$', text, re.MULTILINE) == [("8444", "8444")]
    assert "8766:" not in text
    # The tunnel client reaches 127.0.0.1:8766 by sharing the gateway's network namespace.
    assert 'network_mode: "service:gateway"' in text
    for flag in ("--oauth", "--allowed-host", "--device-listen", "--request-log"):
        assert f"- {flag}" in text
    assert "--simulator" not in text and "--lan-listen" not in text
    # Images are pinned to exact versions.
    assert re.search(r"image: cloudflare/cloudflared:\d{4}\.\d+\.\d+$", text, re.MULTILINE)


def test_tunnel_example_forwards_only_mcp():
    text = (DOCKER / "cloudflared" / "config.yml.example").read_text()
    assert re.findall(r"service: (\S+)", text) == ["http://127.0.0.1:8766", "http_status:404"]
    assert "8443" not in text and "8444" not in text and "8765" not in text


def test_image_build_context_holds_what_the_wheel_needs():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    dockerfile = (DOCKER / "Dockerfile").read_text()
    ignore = (DOCKER / "Dockerfile.dockerignore").read_text().splitlines()
    for path in ["src", *included]:
        assert f"!{path}/" in ignore
        assert f"COPY {path} ./{path}" in dockerfile
    assert "mkdir -p /data/config /data/logs" in dockerfile  # the request log's directory
    assert "USER gadgets" in dockerfile and "uv sync --locked --no-dev" in dockerfile
    assert re.search(r"^FROM python:\d+\.\d+-slim-\w+$", dockerfile, re.MULTILINE)


# The hub's simulator kit ships the source without .gitignore; the rule matters in a checkout.
@pytest.mark.skipif(not (ROOT / ".gitignore").exists(), reason="not a git checkout")
def test_real_tunnel_files_are_ignored():
    rules = (ROOT / ".gitignore").read_text()
    assert "examples/docker/cloudflared/*" in rules
    assert "!examples/docker/cloudflared/config.yml.example" in rules
