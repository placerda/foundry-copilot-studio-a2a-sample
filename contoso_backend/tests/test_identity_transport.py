import json
import threading
import time
from types import SimpleNamespace

import pytest

import auth


class Response:
    status = 200

    def __init__(self, chunks, length=None):
        self.chunks = iter(chunks)
        self.length = length

    def getheader(self, name):
        return self.length

    def read1(self, size):
        return next(self.chunks, b"")


def connection(monkeypatch, response):
    value = SimpleNamespace(sock=None, closed=False)
    value.request = lambda *args, **kwargs: None
    value.getresponse = lambda: response
    value.close = lambda: setattr(value, "closed", True)
    monkeypatch.setattr(auth.http.client, "HTTPSConnection", lambda *args, **kwargs: value)
    return value


def test_transport_reads_close_delimited_json(monkeypatch):
    value = connection(monkeypatch, Response([b'{"keys":', b"[]}", b""]))
    assert auth.fetch_document("https://login.microsoftonline.com/keys", 1) == {"keys": []}
    assert value.closed


@pytest.mark.parametrize("status", [301, 302, 403, 500])
def test_transport_never_follows_redirects_or_errors(monkeypatch, status):
    response = Response([])
    response.status = status
    value = connection(monkeypatch, response)
    with pytest.raises(ValueError):
        auth.fetch_document("https://login.microsoftonline.com/keys", 1)
    assert value.closed


@pytest.mark.parametrize("response", [
    Response([], str(auth.MAX_DOCUMENT_BYTES + 1)),
    Response([b"x" * (auth.MAX_DOCUMENT_BYTES + 1)]),
    Response([b"not-json"]),
    Response([b"\xff"]),
])
def test_transport_rejects_unbounded_or_invalid_documents(monkeypatch, response):
    value = connection(monkeypatch, response)
    with pytest.raises((ValueError, UnicodeError, json.JSONDecodeError)):
        auth.fetch_document("https://login.microsoftonline.com/keys", 1)
    assert value.closed


@pytest.mark.parametrize("stage", ["request", "getresponse", "read1"])
def test_deadline_covers_blocked_dns_headers_and_close_delimited_body(monkeypatch, stage):
    release = threading.Event()
    finished = threading.Event()
    calls = []
    response = Response([b"{}", b""])
    value = connection(monkeypatch, response)
    target = response if stage == "read1" else value
    original = getattr(target, stage)

    def blocked_request(*args, **kwargs):
        calls.append(1)
        release.wait(1)
        finished.set()
        return original(*args, **kwargs)

    setattr(target, stage, blocked_request)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            auth.fetch_document("https://login.microsoftonline.com/keys", 0.03)
        assert time.monotonic() - started < 0.3
        with pytest.raises(TimeoutError):
            auth.fetch_document("https://login.microsoftonline.com/keys", 0.03)
        assert calls == [1], "A stalled transport must not spawn unbounded workers"
    finally:
        release.set()
        assert finished.wait(1)
        # Wait for cleanup before another test uses the transport.
        for _ in range(100):
            if value.closed:
                break
            time.sleep(0.005)
        assert value.closed
