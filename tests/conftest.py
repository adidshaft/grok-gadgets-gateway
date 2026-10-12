import pytest


@pytest.fixture(autouse=True)
def private_home(tmp_path_factory, monkeypatch):
    # Child gateways must never load the operator's real ~/.config/grok-gadgets.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home
