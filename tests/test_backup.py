import os
import stat
import tarfile
from datetime import UTC, datetime

import pytest

from grok_gadgets_gateway import backup, cli
from grok_gadgets_gateway.operator import OperatorError


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "config" / "grok-gadgets"
    (root / "oauth" / "receipts").mkdir(parents=True)
    (root / "credentials.json").write_text('{"devices": {}}')
    (root / "oauth" / "credits-ledger.jsonl").write_text('{"account": "acct_example"}\n')
    (root / "oauth" / "receipts" / "acct_example.jsonl").write_text("{}\n")
    (root / ".credentials.json.abcd1234").write_text("half-written")
    (root / "link").symlink_to(root / "credentials.json")
    return root


def test_backup_archives_the_config_dir_owner_only(config, tmp_path):
    made = backup.create_backup(now=datetime(2026, 10, 24, 6, 0, tzinfo=UTC))
    path = made["path"]
    assert path == tmp_path / "data/grok-gadgets/backups/grok-gadgets-backup-20261024T060000Z.tgz"
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    assert mode(f"{path}.sha256") == 0o600
    assert backup.verify_backup(path)
    assert made["files"] == 3
    with tarfile.open(path) as archive:
        names = archive.getnames()
    assert "grok-gadgets/oauth/credits-ledger.jsonl" in names
    assert "grok-gadgets/oauth/receipts/acct_example.jsonl" in names
    assert "grok-gadgets/link" not in names
    assert "grok-gadgets/.credentials.json.abcd1234" not in names
    assert not list(path.parent.glob(".*partial"))


def test_restore_into_an_empty_config_home_matches(config, tmp_path):
    made = backup.create_backup(tmp_path / "out")
    restored = tmp_path / "restored"
    restored.mkdir()
    with tarfile.open(made["path"]) as archive:
        archive.extractall(restored, filter="data")
    for name in ("credentials.json", "oauth/credits-ledger.jsonl"):
        assert (restored / "grok-gadgets" / name).read_bytes() == (config / name).read_bytes()


def test_checksum_mismatch_is_reported(config, tmp_path):
    path = backup.create_backup(tmp_path / "out")["path"]
    with open(path, "ab") as handle:
        handle.write(b"x")
    assert not backup.verify_backup(path)


def test_keep_prunes_only_the_oldest_backups(config, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    for hour in (1, 2, 3):
        backup.create_backup(out, now=datetime(2026, 10, 24, hour, tzinfo=UTC))
    made = backup.create_backup(out, keep=2, now=datetime(2026, 10, 24, 4, tzinfo=UTC))
    left = sorted(p.name for p in out.iterdir())
    assert [p.name for p in made["pruned"]] == [
        "grok-gadgets-backup-20261024T010000Z.tgz",
        "grok-gadgets-backup-20261024T020000Z.tgz",
    ]
    assert left == [
        "grok-gadgets-backup-20261024T030000Z.tgz",
        "grok-gadgets-backup-20261024T030000Z.tgz.sha256",
        "grok-gadgets-backup-20261024T040000Z.tgz",
        "grok-gadgets-backup-20261024T040000Z.tgz.sha256",
        "notes.txt",
    ]


@pytest.mark.parametrize("inside", ["", "backups"])
def test_backup_folder_must_be_outside_the_config_dir(config, inside):
    with pytest.raises(OperatorError, match="outside the config directory"):
        backup.create_backup(config / inside)


def test_missing_config_dir_is_an_error(tmp_path):
    with pytest.raises(OperatorError, match="no config directory"):
        backup.create_backup(tmp_path / "out", source=tmp_path / "missing")


def test_backup_cli_and_verify(config, tmp_path, capsys):
    out = tmp_path / "out"
    assert cli.main(["backup", "--out", str(out), "--keep", "3"]) == 0
    printed = capsys.readouterr()
    assert "SHA-256:" in printed.out and "holds live tokens" in printed.err
    (path,) = out.glob("*.tgz")
    assert cli.main(["backup", "--verify", str(path)]) == 0
    path.write_bytes(b"changed")
    assert cli.main(["backup", "--verify", str(path)]) == 1
