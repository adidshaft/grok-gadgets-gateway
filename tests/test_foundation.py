from pathlib import Path


def test_foundation():
    root = Path(__file__).parents[1]
    assert "Apache License" in (root / "LICENSE").read_text()
    assert (root / "SECURITY.md").exists()
