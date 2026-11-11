import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
OWNER = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def environment(monkeypatch):
    values = {"TENANT_ID": TENANT, "API_AUDIENCE": AUDIENCE,
              "ALLOWED_USER_OID": OWNER}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    import socket

    def deny(*args, **kwargs):
        raise AssertionError("External networking is forbidden in unit tests")

    monkeypatch.setattr(socket.socket, "connect", deny)
