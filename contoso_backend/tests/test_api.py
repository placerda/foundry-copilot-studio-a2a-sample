import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from contoso_backend.tests.test_local_server import call, no_external_calls, server


class FakeAuth:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure

    def verify(self, token):
        self.calls += 1
        if self.failure:
            raise self.failure
        return {
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "object_id": "33333333-3333-3333-3333-333333333333",
        }


@pytest.mark.parametrize("changes,status", [
    ({}, 200),
    ({"Authorization": ""}, 401),
    ({"Origin": "https://attacker.example"}, 403),
    ({"Content-Type": "text/plain"}, 400),
])
def test_real_http_response_matches_openapi(server, changes, status):
    actual, headers, raw = call(server, changes=changes)
    assert actual == status
    contract = yaml.safe_load(
        (Path(__file__).parents[2] / "contracts" / "chat-api.openapi.yaml")
        .read_text(encoding="utf-8")
    )
    instance = json.loads(raw)
    response = contract["paths"]["/api/chat"]["post"]["responses"][str(status)]
    if "$ref" in response:
        response = contract["components"]["responses"][response["$ref"].rsplit("/", 1)[-1]]
    jsonschema.validate(instance, {
        **response["content"]["application/json"]["schema"],
        "components": contract["components"],
    })
    assert instance["request_id"] == headers["X-Request-ID"]
    assert headers["Cache-Control"] == "no-store"
    assert "/api/chat" in contract["paths"] and "/" not in contract["paths"]
