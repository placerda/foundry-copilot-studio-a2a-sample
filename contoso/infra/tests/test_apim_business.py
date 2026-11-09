"""Offline schema/policy contracts; these do not prove APIM execution."""

from copy import deepcopy
import json
import re
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

from jsonschema import Draft4Validator, ValidationError
import pytest

from contoso.gateway.a2a import validate_a2a_request


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "apim" / "a2a-request.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft4Validator(SCHEMA)
POLICY = ElementTree.parse(ROOT / "apim" / "a2a-policy.xml").getroot()


def test_product_errors_do_not_project_dependency_diagnostics():
    policy = (ROOT / "apim" / "a2a-policy.xml").read_text(encoding="utf-8")
    assert "x-contoso-validation-stage" not in policy
    assert "rpcMessage" not in policy
    assert "rpcError" not in policy
    assert "context.LastError.Source" not in policy
    assert '\"stage\"' not in policy
    failures = POLICY.findall("./outbound/choose/when/return-response/set-body")
    assert len(failures) == 1
    assert json.loads(failures[0].text) == {"error": "contoso_unavailable"}


def test_policy_only_update_retains_operation_and_schema():
    update = (ROOT / "apim-business-policy.bicep").read_text(encoding="utf-8")
    initial = (ROOT / "apim-business.bicep").read_text(encoding="utf-8")
    assert "resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing" in update
    assert "operations@" not in update
    assert "schemas@" not in update
    assert "/endpoint/protocols/invocations?api-version=v1" in update
    assert "/endpoint/protocols/a2a?api-version=v1" not in update
    assert update[update.index("var policyTemplate"):].split("resource apiPolicy")[0] == (
        initial[initial.index("var policyTemplate"):].split("resource requestSchema")[0]
    )


def request(question="What are the delivery terms?"):
    return {
        "jsonrpc": "2.0", "id": str(uuid4()), "method": "SendMessage",
        "params": {
            "message": {"messageId": str(uuid4()), "role": "ROLE_USER",
                        "parts": [{"text": question}]},
            "configuration": {"acceptedOutputModes": ["text/plain"], "returnImmediately": False},
        },
    }


@pytest.mark.parametrize("question", [
    "Delivery terms?", "\nDelivery\tterms?\r\n", "Termos de entrega para São Paulo?",
    "q" * 2000, "🚚" * 2000,
])
def test_request_schema_matches_current_bridge(question):
    Draft4Validator.check_schema(SCHEMA)
    payload = request(question)
    VALIDATOR.validate(payload)
    assert validate_a2a_request(payload) is payload


@pytest.mark.parametrize(("path", "value"), [
    (("jsonrpc",), "1.0"), (("method",), "message/send"), (("id",), "not-a-uuid"),
    (("params", "message", "role"), "ROLE_AGENT"),
    (("params", "message", "messageId"), "uppercase-or-invalid"),
    (("params", "message", "parts"), []),
    (("params", "message", "parts"), [{"text": "one"}, {"text": "two"}]),
    (("params", "message", "parts", 0, "text"), ""),
    (("params", "message", "parts", 0, "text"), " \t\r\n"),
    (("params", "message", "parts", 0, "text"), "q" * 2001),
    (("params", "message", "parts", 0, "text"), "bad\x00control"),
    (("params", "message", "parts", 0, "text"), 23),
    (("params", "configuration", "returnImmediately"), True),
    (("params", "configuration", "returnImmediately"), 0),
    (("params", "configuration", "acceptedOutputModes"), ["application/json"]),
])
def test_invalid_framing_rejected_by_schema_and_bridge(path, value):
    payload = request()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        VALIDATOR.validate(payload)
    with pytest.raises(ValueError):
        validate_a2a_request(payload)


@pytest.mark.parametrize("path", [
    (), ("params",), ("params", "message"), ("params", "message", "parts", 0),
    ("params", "configuration"),
])
def test_no_extra_fields_or_caller_supplied_endpoint(path):
    payload = deepcopy(request())
    target = payload
    for key in path:
        target = target[key]
    target["endpoint"] = "https://attacker.invalid/"
    with pytest.raises(ValidationError):
        VALIDATOR.validate(payload)
    with pytest.raises(ValueError):
        validate_a2a_request(payload)


def test_exact_signed_app_admission_precedes_body_and_workload_token():
    inbound = list(POLICY.find("inbound"))
    names = [child.tag for child in inbound]
    assert names.index("validate-jwt") < names.index("validate-content")
    assert names.index("validate-content") < names.index("authentication-managed-identity")
    jwt = POLICY.find("inbound/validate-jwt")
    assert jwt.attrib["require-signed-tokens"] == "true"
    assert jwt.attrib["require-expiration-time"] == "true"
    assert jwt.findtext("audiences/audience") == "__AUDIENCE__"
    assert jwt.findtext("issuers/issuer") == "https://login.microsoftonline.com/__TENANT__/v2.0"
    caller = POLICY.find("inbound/choose/when").attrib["condition"]
    for binding in ('"scp"', '"azp"', '"oid"', '"roles"', "CONTOSO.Invoke",
                    "__REQUESTER_CLIENT__", "__REQUESTER_PRINCIPAL__"):
        assert binding in caller


def test_operation_declares_json_for_unspecified_content_type_prevention():
    template = (ROOT / "apim-business.bicep").read_text(encoding="utf-8")
    operation = template.split("resource sendMessage ", 1)[1].split("resource apiPolicy ", 1)[0]
    assert re.search(r"request:\s*\{\s*representations:\s*\[\s*\{\s*contentType:\s*'application/json'", operation)
    assert POLICY.find("inbound/validate-content").attrib["unspecified-content-type-action"] == "prevent"


def test_no_endpoint_override_redirect_or_forged_platform_identity():
    assert POLICY.find("inbound/set-backend-service").attrib["base-url"] == "__FOUNDRY_ORIGIN__"
    rewrite = POLICY.find("inbound/rewrite-uri")
    assert rewrite.attrib == {"template": "__ROUTER_PATH__", "copy-unmatched-params": "false"}
    deleted = {header.attrib["name"] for header in POLICY.findall("inbound/set-header")
               if header.attrib["exists-action"] == "delete"}
    assert {"x-agent-foundry-call-id", "x-ms-client-principal", "x-contoso-user-context",
            "x-contoso-identity-mode", "x-ms-token-aad-access-token"} <= deleted
    forward = POLICY.find("backend/forward-request")
    assert forward.attrib["follow-redirects"] == "false"
    assert forward.attrib["timeout"] == "70"
    assert POLICY.find("inbound/authentication-managed-identity").attrib["resource"] == "https://ai.azure.com"
    assert not POLICY.findall(".//trace")
    assert not POLICY.findall(".//cache-store")


def test_duplicate_properties_byte_limits_and_response_ids_guarded():
    body_check = POLICY.find("inbound/set-variable[@name='request-valid']").attrib["value"]
    assert "objects.Peek().Add(key)" in body_check
    assert "JsonConvert.DeserializeObject<string>(token)" in body_check
    assert "32768" in body_check and "8000" in body_check
    response_check = POLICY.find("outbound/set-variable").attrib["value"]
    assert "1048576" in response_check
    assert 'context.Variables["rpc-id"]' in response_check
    assert 'body["result"].Type == JTokenType.Object' in response_check
    assert "contoso_unavailable" in ElementTree.tostring(POLICY, encoding="unicode")


@pytest.mark.parametrize(("raw", "valid"), [
    ('{"id":"one","id":"two"}', False),
    ('{"id":"one","\\u0069d":"two"}', False),
    ('{"params":{"message":{"text":"one","text":"two"}}}', False),
    ('{"parts":[{"text":"one"},{"text":"two"}]}', True),
    ('{"text":"quoted \\"id\\": \\"value\\" with { braces }"}', True),
    ('{"id":"first","nested":{"id":"second"}}', True),
    ('{\n  "id" : "one", \n "id" : "two"}', False),
])
def test_duplicate_guard_split_pattern_preserves_object_boundaries(raw, valid):
    # Exercise the exact regex/stack algorithm, not the APIM expression runtime.
    expression = POLICY.find("inbound/set-variable[@name='request-valid']").attrib["value"]
    match = re.search(r'Regex\.Split\(raw, @"((?:""|[^"])*)"\)', expression)
    assert match
    tokens = re.split(match.group(1).replace('""', '"'), raw)
    objects = []
    duplicate = False
    for i in range(1, len(tokens), 2):
        token = tokens[i]
        if token == "{":
            objects.append(set())
        elif token == "}":
            objects.pop()
        elif i + 2 < len(tokens) and tokens[i + 2] == ":" and token.startswith('"'):
            key = json.loads(token)
            if key in objects[-1]:
                duplicate = True
                break
            objects[-1].add(key)
    assert (not duplicate) is valid


def test_operation_is_not_created_before_authentication_policy():
    template = (ROOT / "apim-business.bicep").read_text(encoding="utf-8")
    operation = template.split("resource sendMessage ", 1)[1].split("resource apiPolicy ", 1)[0]
    assert "dependsOn: [apiPolicy]" in operation
    assert "dependsOn: [requestSchema, sendMessage]" not in template


def test_unknown_operation_preserves_not_found_instead_of_dependency_failure():
    error = POLICY.find("on-error/return-response")
    status = error.find("set-status").attrib["code"]
    assert 'context.LastError.Reason == "OperationNotFound" ? 404' in status
    assert '"error\\":\\"not_found' in error.findtext("set-body")
