from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).parents[2]


def template():
    return yaml.safe_load((ROOT / "contoso-template.yaml").read_text(encoding="utf-8"))


def test_runtime_and_scope():
    value = template()
    assert value["Transform"] == "AWS::Serverless-2016-10-31"
    resources = value["Resources"]
    assert {item["Type"] for item in resources.values()} == {
        "AWS::Serverless::Function", "AWS::IAM::Role", "AWS::Logs::LogGroup"}
    function = resources["ChatFunction"]["Properties"]
    assert function["Runtime"] == "python3.12"
    assert function["Architectures"] == ["arm64"]
    assert function["MemorySize"] == 512
    assert function["Timeout"] == {"Fn::If": ["UseCONTOSO", 90, 30]}
    assert function["ReservedConcurrentExecutions"] == 2
    assert function["Handler"] == "app.lambda_handler"
    assert "Policies" not in function
    assert resources["ChatLogGroup"]["Properties"]["RetentionInDays"] == 3


def test_url_and_role():
    resources = template()["Resources"]
    url = resources["ChatFunction"]["Properties"]["FunctionUrlConfig"]
    assert url["AuthType"] == "NONE"
    assert url["InvokeMode"] == "BUFFERED"
    assert url["Cors"] == {"AllowOrigins": ["http://localhost:8000"],
                          "AllowMethods": ["POST"], "AllowHeaders": ["Authorization", "Content-Type"],
                          "AllowCredentials": False}
    # Function URL answers preflight itself; OPTIONS is not an allowed Cors enum.
    role = resources["ChatRole"]["Properties"]
    assert "ManagedPolicyArns" not in role
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    assert len(statements) == 2
    assert statements[0]["Action"] == ["bedrock:InvokeModel"]
    assert statements[0]["Resource"]["Fn::Sub"] == (
        "arn:${AWS::Partition}:bedrock:us-east-2::foundation-model/${BedrockModelId}")
    assert set(statements[1]["Action"]) == {"logs:CreateLogStream", "logs:PutLogEvents"}
    assert statements[1]["Resource"] == {"Fn::GetAtt": ["ChatLogGroup", "Arn"]}


def test_parameters_and_no_tracing():
    value = template()
    for name in ("TenantId", "ApiAudience", "AllowedUserOid"):
        assert "Default" not in value["Parameters"][name]
        assert "AllowedPattern" in value["Parameters"][name]
    assert value["Parameters"]["BedrockModelId"]["AllowedValues"] == ["amazon.nova-lite-v1:0"]
    assert value["Parameters"]["MaxMessages"]["Default"] == 10
    assert value["Parameters"]["MaxOutputTokens"]["Default"] == 400
    variables = value["Resources"]["ChatFunction"]["Properties"]["Environment"]["Variables"]
    assert variables["LANGCHAIN_TRACING_V2"] == "false"
    assert variables["LANGSMITH_TRACING"] == "false"
    assert value["Rules"]["ProjectRegion"]["Assertions"][0]["Assert"] == {
        "Fn::Equals": [{"Ref": "AWS::Region"}, "us-east-2"]}


def test_contoso_is_opt_in_and_secret_is_protected():
    value = template()
    assert value["Conditions"]["UseCONTOSO"] == {"Fn::Equals": [{"Ref": "CONTOSOEnabled"}, "true"]}
    assert value["Parameters"]["CONTOSOEnabled"]["Default"] == "false"
    assert "CONTOSORequesterClientSecret" not in value["Parameters"]
    secret = value["Parameters"]["CONTOSORequesterSecretName"]
    assert secret["Default"] == ""
    assert "NoEcho" not in secret
    variables = value["Resources"]["ChatFunction"]["Properties"]["Environment"]["Variables"]
    for name, parameter in [
        ("CONTOSO_ENABLED", "CONTOSOEnabled"), ("CONTOSO_GATEWAY_URL", "CONTOSOGatewayURL"),
        ("CONTOSO_ENTRA_TENANT_ID", "CONTOSOEntraTenantId"),
        ("CONTOSO_GATEWAY_API_CLIENT_ID", "CONTOSOGatewayApiClientId"),
        ("CONTOSO_REQUESTER_CLIENT_ID", "CONTOSORequesterClientId"),
    ]:
        assert variables[name] == {"Ref": parameter}
    assert variables["CONTOSO_REQUESTER_CLIENT_SECRET"] == {
        "Fn::If": [
            "UseCONTOSO",
            {"Fn::Sub": "{{resolve:secretsmanager:${CONTOSORequesterSecretName}:SecretString:client_secret}}"},
            "",
        ],
    }


def test_enabled_integration_requires_a_protected_credential_reference():
    rule = template()["Rules"]["CONTOSOProtectedCredential"]
    assert rule["RuleCondition"] == {"Fn::Equals": [{"Ref": "CONTOSOEnabled"}, "true"]}
    assert rule["Assertions"][0]["Assert"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "CONTOSORequesterSecretName"}, ""]}],
    }


@pytest.mark.parametrize("name,allowed", [
    ("", True),
    ("contoso-agent-demo/requester", True),
    ("contoso-other-demo/requester", True),
    ("contoso-agent-demo/requester:SecretString:another_key", False),
    ("contoso-agent-demo/requester:AWSPREVIOUS", False),
    ("arn:aws:secretsmanager:us-east-1:123456789012:secret:other", False),
    ("unrelated-project/requester", False),
    ("contoso-agent-demo/requester\n", False),
])
def test_requester_secret_name_cannot_change_reference_scope_or_json_key(name, allowed):
    parameter = template()["Parameters"]["CONTOSORequesterSecretName"]
    assert (re.fullmatch(parameter["AllowedPattern"], name) is not None) == allowed
