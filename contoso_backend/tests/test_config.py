import pytest
from config import Settings


def test_defaults(environment):
    settings = Settings.from_env(environment)
    assert settings.region == "us-east-2"
    assert settings.model_id == "amazon.nova-lite-v1:0"
    assert settings.max_messages == 10
    assert settings.max_output_tokens == 400


@pytest.mark.parametrize("name,value", [
    ("TENANT_ID", ""), ("API_AUDIENCE", "not-guid"),
    ("ALLOWED_USER_OID", ""), ("AWS_REGION", "us-east-1"),
    ("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0"),
    ("BEDROCK_MODEL_ID", "global.model"),
    ("BEDROCK_MODEL_ID", "arn:aws:bedrock:us-east-1::foundation-model/test"),
    ("MAX_MESSAGES", "0"), ("MAX_OUTPUT_TOKENS", "-1"),
    ("MAX_OUTPUT_TOKENS", "true"), ("REQUIRED_SCOPE", "a b"),
])
def test_invalid(environment, name, value):
    with pytest.raises(ValueError):
        Settings.from_env({**environment, name: value})
