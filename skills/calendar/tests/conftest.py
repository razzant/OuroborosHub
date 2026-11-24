"""Calendar's own tests never inherit a live Host Service address or skill token from the shell that runs them.

A test that talks to the Host Service sets both itself (patch.dict or monkeypatch.setenv); every other test sees neither.
"""
import pytest


@pytest.fixture(autouse=True)
def no_inherited_host_service(monkeypatch):
    for name in ('HOST_SERVICE_URL', 'HOST_SERVICE_TOKEN'):
        monkeypatch.delenv(name, raising=False)
