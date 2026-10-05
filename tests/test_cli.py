import subprocess
import sys

import pytest


def gateway(*args, env=None, timeout=10):
    return subprocess.run(
        [sys.executable, "-m", "grok_gadgets_gateway.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


@pytest.mark.parametrize(
    "text,mode,expected",
    [
        ('{"devices": {"a": {"token": "0123456789abcdef", "revoked": "no"}}}', 0o600, "revoked"),
        ('{"devices": {}, "devices": {}}', 0o600, "duplicate key"),
        ('{"devices": {}}', 0o644, "mode 0600"),
    ],
)
def test_invalid_credentials_fail_at_startup_without_echo(tmp_path, text, mode, expected):
    path = tmp_path / "credentials.json"
    path.write_text(text)
    path.chmod(mode)
    result = gateway("--credentials", str(path))
    assert result.returncode == 2 and result.stdout == ""
    assert expected in result.stderr and "0123456789abcdef" not in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_explicit_credentials_fail_at_startup(tmp_path):
    result = gateway("--credentials", str(tmp_path / "missing.json"))
    assert result.returncode == 2 and "does not exist" in result.stderr


def isolated_env(tmp_path):
    import os

    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(tmp_path)
    env.pop("GROK_GADGETS_DEVICE_TOKEN", None)
    env.pop("GROK_GADGETS_MCP_TOKEN", None)
    return env


def test_init_enroll_devices_revoke_and_rotate_are_atomic(tmp_path):
    import json
    import stat

    env = isolated_env(tmp_path)
    created = gateway("init", env=env)
    assert created.returncode == 0 and created.stdout == ""
    assert "GROK_GADGETS_DEVICE_TOKEN" not in created.stderr
    root = tmp_path / "grok-gadgets"
    credentials = root / "credentials.json"
    mcp_token = root / "mcp-token"
    assert (credentials.stat().st_mode & 0o777) == 0o600
    assert (mcp_token.stat().st_mode & 0o777) == 0o600
    assert (root.stat().st_mode & 0o777) == 0o700

    enrolled = gateway("enroll", "atoms3-lite-1", env=env)
    assert enrolled.returncode == 0
    assert enrolled.stdout.startswith("GROK_GADGETS_DEVICE_TOKEN=")
    token = enrolled.stdout.strip().split("=", 1)[1]
    assert token not in enrolled.stderr and "Give this token" in enrolled.stderr
    again = gateway("enroll", "atoms3-lite-1", env=env)
    assert again.returncode == 2 and token not in again.stderr + again.stdout
    assert gateway("enroll", "bad id", env=env).returncode == 2
    listed = gateway("devices", env=env)
    assert listed.stdout == "atoms3-lite-1\n" and token not in listed.stdout

    stored = json.loads(credentials.read_text())
    assert stored["devices"]["atoms3-lite-1"]["token"] == token
    assert stored["devices"]["atoms3-lite-1"]["revoked"] is False
    revoked = gateway("revoke", "atoms3-lite-1", env=env)
    assert revoked.returncode == 0 and token not in revoked.stdout + revoked.stderr
    assert json.loads(credentials.read_text())["devices"]["atoms3-lite-1"]["revoked"] is True
    assert list(root.glob(".credentials.json.*")) == []

    rotated = gateway("rotate-mcp-token", env=env)
    assert rotated.returncode == 0
    mcp = rotated.stdout.strip().split("=", 1)[1]
    assert rotated.stdout.startswith("GROK_GADGETS_MCP_TOKEN=")
    assert mcp not in rotated.stderr and token not in rotated.stdout
    assert mcp_token.read_text().strip() == mcp
    assert (mcp_token.stat().st_mode & stat.S_IRWXG) == 0
    kept = gateway("init", env=env)
    assert kept.returncode == 0 and "Existing files kept" in kept.stderr
    assert mcp_token.read_text().strip() == mcp


def test_serve_help_and_missing_config(tmp_path):
    env = isolated_env(tmp_path)
    help_text = gateway("serve", "--help", env=env)
    assert help_text.returncode == 0
    assert "8766" in help_text.stdout and "--allowed-host" in help_text.stdout
    missing = gateway("serve", "--port", "9", env=env)
    assert missing.returncode == 2 and missing.stdout == ""
    assert "does not exist" in missing.stderr
    stdio_help = gateway("--help", env=env)
    assert stdio_help.returncode == 0 and "--simulator" in stdio_help.stdout
    assert "--test-controls" in stdio_help.stdout


def test_stdio_uses_xdg_credentials_when_present(tmp_path, monkeypatch):
    from grok_gadgets_gateway.cli import resolve_credentials

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert resolve_credentials(None) is None
    path = tmp_path / "grok-gadgets"
    path.mkdir()
    registry = path / "credentials.json"
    registry.write_text("{}\n")
    assert resolve_credentials(None) == str(registry)
    assert resolve_credentials(str(tmp_path / "other.json")) == str(tmp_path / "other.json")
