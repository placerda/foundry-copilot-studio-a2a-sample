import pytest

from contoso.configuration import RouterSettings


@pytest.fixture
def router_env():
    return {
        "CONTOSO_RUNTIME_ROLE": "router",
        "CONTOSO_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "CONTOSO_ENTRA_API_CLIENT_ID": "22222222-2222-4222-8222-222222222222",
        "CONTOSO_STUDIO_ENVIRONMENT_ID": "33333333-3333-4333-8333-333333333333",
        "CONTOSO_STUDIO_STANDARD_SCHEMA": "demoStandard",
        "CONTOSO_STUDIO_HARNESS_ENDPOINT": "https://api.powerplatform.com/copilotstudio-a2a/demo",
        "CONTOSO_KNOWLEDGE_A2A_ENDPOINT": (
            "https://selected-foundry.services.ai.azure.com/api/projects/demo/agents/knowledge/a2a"
        ),
        "CONTOSO_GATEWAY_URL": "https://demo-gateway.azurewebsites.net",
        "CONTOSO_PLATFORM_CALLER_IDS": '["approved-service"]',
    }


@pytest.fixture
def router_settings(router_env):
    return RouterSettings.from_environment(router_env)
