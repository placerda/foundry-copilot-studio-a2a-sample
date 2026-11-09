"""Operator-only fixed private diagnostic, not a business or token API."""

from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
POLICY = ElementTree.parse(ROOT / "apim" / "operator-probe-policy.xml").getroot()
TEMPLATE = (ROOT / "apim-operator-probe.bicep").read_text(encoding="utf-8")


def test_policy_only_update_preserves_existing_api_metadata():
    update = (ROOT / "apim-operator-policy.bicep").read_text(encoding="utf-8")
    assert "resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing" in update
    assert "operations@" not in update
    assert update[update.index("var template"):].split("resource apiPolicy")[0] == (
        TEMPLATE[TEMPLATE.index("var template"):].split("resource api ")[0]
    )


def test_exact_delegated_operator_not_requester_or_app_roles():
    jwt = POLICY.find("inbound/validate-jwt")
    assert jwt.attrib["require-signed-tokens"] == "true"
    assert jwt.attrib["require-expiration-time"] == "true"
    assert jwt.findtext("audiences/audience") == "__AUDIENCE__"
    admission = POLICY.find("inbound/choose/when").attrib["condition"]
    for value in ('"azp"', '"oid"', '"scp"', "__OPERATOR_CLIENT__", "__OPERATOR_PRINCIPAL__", "CONTOSO.Deploy"):
        assert value in admission
    assert "CONTOSO.Invoke" not in admission
    assert '"roles"' not in admission


def test_operator_can_choose_neither_endpoint_nor_business_question():
    gate = POLICY.findall("inbound/choose/when")[1].attrib["condition"]
    for value in ('raw.Trim() != "{}"', "Query.Count", '"Content-Encoding"', '"Content-Type"'):
        assert value in gate
    assert "CONTOSO identity probe" in POLICY.findtext("inbound/set-body")
    rewrite = POLICY.find("inbound/rewrite-uri")
    assert rewrite.attrib["copy-unmatched-params"] == "false"
    assert "agents/contoso-foundry-sdk-agent/endpoint/protocols/openai/responses?api-version=v1" in TEMPLATE
    assert POLICY.findtext("inbound/set-body") == '{"input":"CONTOSO identity probe","store":false}'


def test_private_probe_uses_only_apim_workload_and_no_redirects():
    token = POLICY.find("inbound/authentication-managed-identity")
    assert token.attrib["resource"] == "https://ai.azure.com"
    assert token.attrib["ignore-error"] == "false"
    forward = POLICY.find("backend/forward-request")
    assert forward.attrib["follow-redirects"] == "false"
    assert forward.attrib["timeout"] == "40"
    assert POLICY.find("inbound/cache-lookup") is None
    assert POLICY.find("inbound/trace") is None


def test_only_bounded_identity_metadata_is_returned():
    projection = POLICY.findtext("outbound/set-body")
    assert "16384" in projection and "caller.Length > 256" in projection
    assert '["knowledge_access"] != false' in projection
    returned = projection.split("return JsonConvert.SerializeObject(new {", 1)[1].split("});", 1)[0]
    assert "platform_caller_id" in returned
    assert "platform_call_id" not in returned
    assert "token" not in returned
    assert "dependsOn: [apiPolicy]" in TEMPLATE


def test_operator_projection_requires_completed_sdk_responses_output():
    projection = POLICY.findtext("outbound/set-body")
    assert 'body["status"] != "completed"' in projection
    assert 'body["output"].Count() != 1' in projection
    assert 'content[0]["type"] != "output_text"' in projection
    assert 'content[0]["text"].Type != JTokenType.String' in projection


def test_isolated_sdk_proof_projects_only_verified_allowlisted_metadata():
    projection = POLICY.findtext("outbound/set-body")
    isolated = projection.split('if ((string)probe["status"] == "sdk-s2s-proof")', 1)[1]
    isolated = isolated.split('if ((string)probe["status"] != "identity-bootstrap")', 1)[0]
    for required in (
        "workload-identity-mismatch", "application-permission-missing",
        '["signature_validated"]', '["sdk_identity_matched"]',
        '["required_role_present"]', '["application_only"]',
        '["response_sha256"]', "65536", "__SDK_EXPECTED_PRINCIPAL__",
    ):
        assert required in isolated
    for forbidden in ('["token"]', '["access_token"]', '["refresh_token"]',
                      '["conversation_id"]', '["text"]', '["error_message"]'):
        assert forbidden not in isolated
    assert "return result.ToString(Newtonsoft.Json.Formatting.None)" in isolated


def test_sdk_expected_identity_is_a_required_trusted_deployment_binding():
    for name in ("apim-operator-probe.bicep", "apim-operator-policy.bicep"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "param sdkExpectedPrincipalId string\n" in text
        assert "param sdkExpectedPrincipalId string =" not in text
        assert "'__SDK_EXPECTED_PRINCIPAL__', sdkExpectedPrincipalId)" in text
    projection = POLICY.findtext("outbound/set-body")
    assert '(string)probe["expected_sdk_identity"] != "__SDK_EXPECTED_PRINCIPAL__"' in projection
    assert '["expected_sdk_identity"] = "__SDK_EXPECTED_PRINCIPAL__"' in projection


def test_proof_rejections_return_only_fixed_checkpoints_not_upstream_errors():
    projection = POLICY.findtext("outbound/set-body")
    assert 'var checkpoint = "response-json"' in projection
    assert '["proof_stage"] = checkpoint' in projection
    assert '["hosted_status"] = states.Contains(state) ? state : "unknown"' in projection
    assert '["proof_type"] = types.Contains(type) ? type : "unknown"' in projection
    assert '["metadata_source"] = "issued-token-diagnostic"' in projection
    assert '["signature_validated"] = false' in projection
    assert '(bool)identity["resource_authorized"] != (stage == "completed")' in projection
    assert 'operation == "start-conversation" || operation == "ask-question"' in projection
    assert 'statusCode >= 400 && statusCode <= 599' in projection
    for checkpoint in ("proof-json", "identity-projection", "conversation-projection",
                       "error-type-projection", "proof-metadata", "identity-metadata",
                       "conversation-metadata", "unexpected-proof-type"):
        assert checkpoint in projection
    assert 'body["error"]' not in projection
    assert "catch (Exception" not in projection
    assert "Exception.Message" not in projection


def test_router_probe_inherits_operator_auth_admission_and_workload_transport():
    policy = ElementTree.parse(ROOT / "apim" / "operator-router-probe-policy.xml").getroot()
    assert policy.find("inbound")[0].tag == "base"
    assert policy.find("backend/base") is not None
    assert policy.find("on-error/base") is not None
    assert policy.find("inbound/set-backend-service") is None
    assert policy.find("inbound/authentication-managed-identity") is None
    assert policy.find("outbound/base") is None
    assert "CONTOSO identity probe" in policy.findtext("inbound/set-body")
    assert "context.Request.Body" not in policy.findtext("inbound/set-body")
    assert policy.find("inbound/rewrite-uri").attrib["copy-unmatched-params"] == "false"


def test_sdk_transport_projection_is_strict_typed_allowlisted_and_bounded():
    projection = POLICY.findtext("outbound/set-body")
    block = projection.split('if (stage == "studio-failed" || stage == "completed")', 1)[1]
    block = block.split("if (!String.IsNullOrEmpty(errorType)", 1)[0]
    for required in (
        'methodToken.Type == JTokenType.String',
        'method == "GET" || method == "POST" || method == "other"',
        'probe["studio_redirect_statuses"] as JArray', "redirects.Take(8)",
        "item.Type == JTokenType.Integer",
        "code == 301 || code == 302 || code == 303 || code == 307 || code == 308",
        'result["studio_final_method"] = method',
        'result["studio_redirect_statuses"] = statuses',
    ):
        assert required in block
    for forbidden in ("history", "request_info", "headers", "Location", "url",
                      '["token"]', '["body"]', '["message"]'):
        assert forbidden not in block


def test_router_probe_projects_only_allowlisted_structural_metadata():
    policy = ElementTree.parse(ROOT / "apim" / "operator-router-probe-policy.xml").getroot()
    projection = policy.findtext("outbound/set-body")
    assert "65536" in projection
    assert "known.Contains(n)" in projection
    returned = projection.split("upstream_status = status, shape = \"object\",", 1)[1].split("});", 1)[0]
    for field in ("known_root_keys", "response_id_matches", "result_shape", "rpc_error_class"):
        assert field in returned
    for forbidden in ("raw", "token =", "caller =", "text =", "headers =", "body =", "message ="):
        assert forbidden not in returned
    assert policy.find("inbound/trace") is None
    assert policy.findtext("outbound/set-header[@name='Cache-Control']/value") == "no-store"


def test_router_probe_adds_only_an_operation_and_policy_to_existing_operator_api():
    template = (ROOT / "apim-operator-router-probe.bicep").read_text(encoding="utf-8")
    assert "resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing" in template
    assert "name: 'contoso-operator-probe'" in template
    assert "method: 'POST'" in template and "urlTemplate: '/router-framing'" in template
    assert "endpoint/protocols/invocations?api-version=v1" in template
    for forbidden in ("roleAssignments@", "sku:", "serviceUrl:", "listSecrets", "/openai/"):
        assert forbidden not in template


def test_router_error_category_requires_exact_known_handler_error_without_exposing_it():
    policy = ElementTree.parse(ROOT / "apim" / "operator-router-probe-policy.xml").getroot()
    projection = policy.findtext("outbound/set-body")
    assert 'var category = "unclassified"' in projection
    assert 'errorClass == "-32602"' in projection
    assert 'error["message"].Type == JTokenType.String' in projection
    assert '(string)error["message"] == "The request or platform caller is not authorized"' in projection
    assert 'category = "router-admission-or-configuration"' in projection
    returned = projection.split('upstream_status = status, shape = "object",', 1)[1].split("});", 1)[0]
    assert "rpc_error_category = category" in returned
    assert 'error["message"]' not in returned
    assert 'error["data"]' not in projection
