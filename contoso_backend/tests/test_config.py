import pytest
from config import Settings


def test_defaults(environment):
    settings = Settings.from_env(environment)
    assert settings.scope == "access_as_user"
    assert settings.max_messages == 10


@pytest.mark.parametrize("name,value", [
    ("TENANT_ID", ""), ("API_AUDIENCE", "not-guid"),
    ("ALLOWED_USER_OID", ""), ("MAX_MESSAGES", "0"),
    ("MAX_MESSAGES", "true"), ("REQUIRED_SCOPE", "a b"),
])
def test_invalid(environment, name, value):
    with pytest.raises(ValueError):
        Settings.from_env({**environment, name: value})
