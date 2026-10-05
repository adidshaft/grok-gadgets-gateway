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
