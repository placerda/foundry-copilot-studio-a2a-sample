import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ServiceRequestError

from contoso.knowledge import ATTRIBUTION, KnowledgeHost, QUESTION
from contoso.router.core import AccessDenied, CatalogError
from contoso.providers import TransportError


SOURCE = (Path(__file__).resolve().parents[2] / "contoso-demo" / "knowledge"
          / "knowledge-returnables.md").read_bytes()


class Blob:
    def __init__(self, content=SOURCE):
        self.content = content
        self.calls = []

    def get_blob_properties(self):
        self.calls.append("properties")
        return SimpleNamespace(size=len(self.content), etag='"v1"')

    def download_blob(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def readall(self):
        return self.content


class Context:
    user_id = "trusted-library"

    def platform_headers(self):
        return {"x-agent-foundry-call-id": "fresh-call"}


def make(blob=None, context=None):
    return KnowledgeHost(
        blob or Blob(),
        source_sha256=hashlib.sha256(SOURCE).hexdigest(),
        allowed_callers=frozenset({"trusted-library"}),
        context_factory=lambda: context or Context(),
    )


def test_reads_exact_private_source_with_pinned_etag():
    blob = Blob()
    answer = asyncio.run(make(blob).answer({"input": QUESTION}, asyncio.Event()))
    assert answer == SOURCE.decode("utf-8") + ATTRIBUTION
    assert blob.calls == [
        "properties",
        {"etag": '"v1"', "match_condition": MatchConditions.IfNotModified, "max_concurrency": 1},
    ]


def test_unauthorized_caller_cannot_read_source():
    blob = Blob()
    context = Context()
    context.user_id = "foreign"
    with pytest.raises(AccessDenied):
        asyncio.run(make(blob, context).answer({"input": QUESTION}, asyncio.Event()))
    assert blob.calls == []


@pytest.mark.parametrize("question", ["Another question", '{"question":"x","studio_variant":"standard"}'])
def test_unregistered_question_cannot_read_source(question):
    blob = Blob()
    with pytest.raises(CatalogError):
        asyncio.run(make(blob).answer({"input": question}, asyncio.Event()))
    assert blob.calls == []


def test_changed_source_is_not_a_success_answer():
    with pytest.raises(CatalogError, match="approved version"):
        asyncio.run(make(Blob(SOURCE + b"\nChanged")).answer({"input": QUESTION}, asyncio.Event()))


def test_source_unavailable_is_redacted_and_reader_releases():
    class Unavailable(Blob):
        def get_blob_properties(self):
            raise ServiceRequestError("sensitive transport detail")

    provider = make(Unavailable())
    for _ in range(2):
        with pytest.raises(TransportError, match="private approved source is unavailable"):
            asyncio.run(provider.answer({"input": QUESTION}, asyncio.Event()))


def test_cancelled_request_cannot_read_source():
    cancelled = asyncio.Event()
    cancelled.set()
    blob = Blob()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(make(blob).answer({"input": QUESTION}, cancelled))
    assert blob.calls == []


def test_actual_sdk_host_registers():
    host = make().build_host()
    assert host is not None
