from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from azure.ai.projects.models import AgentVersionDetails
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
import pytest

from contoso.ops import create_prompt
from contoso.providers import RawReply, extract_citations
from contoso.router.core import (
    AuthMetadata, AuthScheme, Capability, CatalogEntry, Protocol, SourceMetadata,
)


def version(definition, number="1"):
    return AgentVersionDetails({
        "name": create_prompt.AGENT_NAME, "version": number,
        "definition": definition,
    })


def test_preview_embeds_the_actual_reference_without_acquiring_credentials(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("A preview cannot acquire credentials or call Foundry")

    monkeypatch.setattr(create_prompt, "AzureCliCredential", forbidden)
    monkeypatch.setattr(create_prompt, "AIProjectClient", forbidden)
    assert create_prompt.main(["--model-deployment", "reviewed-model"]) == 0
    result = json.loads(capsys.readouterr().out)
    definition = result["definition"]
    source = (create_prompt.ROOT / create_prompt.SOURCE).read_text(encoding="utf-8").strip()
    assert source in definition["instructions"]
    assert definition["model"] == "reviewed-model"
    assert definition["kind"] == "prompt"
    assert definition["reasoning"] == {"effort": "minimal"}
    assert set(definition) == {"model", "kind", "instructions", "reasoning"}
    assert result["status"] == "local-preview"


def test_prompt_source_line_examples_bind_to_the_embedded_reference():
    source = (create_prompt.ROOT / create_prompt.SOURCE).read_text(encoding="utf-8")
    sections = frozenset(line[3:] for line in source.splitlines() if line.startswith("## "))
    entry = CatalogEntry(
        provider_id="motors-logistics", capability=Capability.KNOWLEDGE_KNOWLEDGE,
        variant=None, protocol=Protocol.A2A,
        endpoint="https://apim.example/agents/motors-logistics/versions/1/a2a",
        auth=AuthMetadata(AuthScheme.ENTRA_BEARER, "demo-tenant",
                          "https://ai.azure.com", frozenset({"https://ai.azure.com/.default"})),
        sources=(SourceMetadata(
            "CONTOSO-MOTORS-LOGISTICS-001", "motors-logistics", "1",
            "Synthetic logistics reference", "synthetic", "embedded reference", sections,
        ),),
    )
    prompt = (create_prompt.ROOT / create_prompt.INSTRUCTIONS).read_text(encoding="utf-8")
    examples = [line for line in prompt.splitlines() if line.startswith("Source: ")]
    assert examples
    for example in examples:
        reply = RawReply(
            example, entry.provider_id, entry.capability, entry.variant,
            entry.protocol, "native-a2a", "TASK_STATE_COMPLETED", entry.endpoint,
        )
        citations = extract_citations(entry, reply)
        assert citations
        assert "Evidence limits" in {citation.section for citation in citations}
        assert {citation.section for citation in citations} <= sections


def test_missing_agent_is_created_separately_and_read_back():
    definition = create_prompt.build_definition("reviewed-model")
    stored = version(definition.as_dict())
    agents = Mock()
    agents.get.side_effect = ResourceNotFoundError()
    agents.create_version.return_value = stored
    agents.get_version.return_value = stored
    result = create_prompt.create_or_verify(SimpleNamespace(agents=agents), definition)
    assert result["status"] == "created"
    agents.create_version.assert_called_once_with(
        create_prompt.AGENT_NAME, definition=definition,
        metadata=create_prompt.definition_metadata(definition),
    )
    agents.get_version.assert_called_once_with(create_prompt.AGENT_NAME, "1")


def test_identical_existing_agent_does_not_create_another_version():
    definition = create_prompt.build_definition("reviewed-model")
    agents = Mock()
    agents.get.return_value.versions.latest = version(definition.as_dict(), "3")
    result = create_prompt.create_or_verify(SimpleNamespace(agents=agents), definition)
    assert result["status"] == "verified-existing"
    assert result["version"] == "3"
    agents.create_version.assert_not_called()
    agents.get_version.assert_not_called()


@pytest.mark.parametrize("change", [
    {"kind": "hosted", "cpu": "1", "memory": "2Gi"},
    {"model": "another-model"},
    {"instructions": "A different business contract"},
    {"tools": [{"type": "web_search_preview"}]},
    {"reasoning": {"effort": "medium"}},
])
def test_conflicting_agent_is_not_updated_or_converted(change):
    definition = create_prompt.build_definition("reviewed-model")
    different = deepcopy(definition.as_dict())
    different.update(change)
    agents = Mock()
    agents.get.return_value.versions.latest = version(different)
    with pytest.raises(RuntimeError, match="different or unverified"):
        create_prompt.create_or_verify(SimpleNamespace(agents=agents), definition)
    agents.create_version.assert_not_called()


def test_permission_failure_is_not_treated_as_an_absent_agent():
    agents = Mock()
    agents.get.side_effect = HttpResponseError("private-upstream-detail")
    with pytest.raises(HttpResponseError):
        create_prompt.create_or_verify(
            SimpleNamespace(agents=agents), create_prompt.build_definition("reviewed-model"),
        )
    agents.create_version.assert_not_called()


def test_existing_agent_without_a_latest_version_is_not_updated():
    agents = Mock()
    agents.get.return_value.versions.latest = None
    with pytest.raises(RuntimeError, match="different or unverified"):
        create_prompt.create_or_verify(
            SimpleNamespace(agents=agents), create_prompt.build_definition("reviewed-model"),
        )
    agents.create_version.assert_not_called()


def test_creation_requires_an_independent_matching_read_back():
    definition = create_prompt.build_definition("reviewed-model")
    agents = Mock()
    agents.get.side_effect = ResourceNotFoundError()
    agents.create_version.return_value = version(definition.as_dict())
    different = definition.as_dict() | {"instructions": "Unexpected service definition"}
    agents.get_version.return_value = version(different)
    with pytest.raises(RuntimeError, match="different or unverified"):
        create_prompt.create_or_verify(SimpleNamespace(agents=agents), definition)
    assert agents.create_version.call_count == 1


@pytest.mark.parametrize("replacement", ["Version: 2", "Version: 10"])
def test_mismatched_source_version_cannot_be_embedded(tmp_path, replacement):
    for relative in (create_prompt.INSTRUCTIONS, create_prompt.SOURCE):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (create_prompt.ROOT / relative).read_text(encoding="utf-8")
        target.write_text(text.replace("Version: 1", replacement), encoding="utf-8")
    with pytest.raises(ValueError, match="citation contract"):
        create_prompt.build_definition("reviewed-model", root=tmp_path)


def test_apply_cannot_fall_back_to_the_default_cli_profile(monkeypatch, capsys):
    monkeypatch.delenv("AZURE_CONFIG_DIR", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("An unselected CLI profile cannot be used")

    monkeypatch.setattr(create_prompt, "AzureCliCredential", forbidden)
    with pytest.raises(SystemExit) as error:
        create_prompt.main([
            "--model-deployment", "reviewed-model", "--apply",
            "--project-endpoint", "https://approved.services.ai.azure.com/api/projects/demo",
            "--tenant-id", "11111111-1111-4111-8111-111111111111",
        ])
    assert error.value.code == 2
    assert "AZURE_CONFIG_DIR" in capsys.readouterr().err


def test_apply_pins_cli_tenant_and_uses_the_explicit_private_project(monkeypatch, tmp_path, capsys):
    endpoint = "https://approved.services.ai.azure.com/api/projects/demo"
    tenant = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setenv("AZURE_CONFIG_DIR", str(tmp_path))
    credential_factory = MagicMock()
    credential = credential_factory.return_value.__enter__.return_value
    client_factory = MagicMock()
    client = client_factory.return_value.__enter__.return_value
    client.agents.get.return_value.versions.latest = version(
        create_prompt.build_definition("reviewed-model").as_dict(),
    )
    monkeypatch.setattr(create_prompt, "AzureCliCredential", credential_factory)
    monkeypatch.setattr(create_prompt, "AIProjectClient", client_factory)
    assert create_prompt.main([
        "--model-deployment", "reviewed-model", "--apply",
        "--project-endpoint", endpoint, "--tenant-id", tenant,
    ]) == 0
    credential_factory.assert_called_once_with(tenant_id=tenant, process_timeout=15)
    options = client_factory.call_args.kwargs
    assert options["endpoint"] == endpoint
    assert options["credential"] is credential
    assert options["transport"].session.trust_env is False
    assert options["retry_total"] == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified-existing"
    client.agents.create_version.assert_not_called()


def a2a_agent(*, enabled=False):
    endpoint = {
        "protocols": ["responses"],
        "protocol_configuration": {"responses": {}},
        "version_selector": {"version_selection_rules": [
            {"type": "FixedRatio", "agent_version": "@latest", "traffic_percentage": 100},
        ]},
        "authorization_schemes": [
            {"type": "Entra", "isolation_key_source": {"kind": "Entra"}},
        ],
    }
    agent = {
        "name": create_prompt.AGENT_NAME, "state": "enabled",
        "versions": {"latest": {"version": "1"}}, "agent_endpoint": endpoint,
    }
    if enabled:
        agent["agent_card"] = json.loads(
            (create_prompt.ROOT / create_prompt.CARD).read_text(encoding="utf-8"),
        )
        endpoint["protocols"].append("a2a")
        endpoint["protocol_configuration"]["a2a"] = {}
    return agent


def a2a_client(before, after):
    definition = create_prompt.build_definition("reviewed-model")
    agents = Mock()
    agents.get.side_effect = [
        SimpleNamespace(as_dict=lambda: deepcopy(before)),
        SimpleNamespace(as_dict=lambda: deepcopy(after)),
    ]
    agents.get_version.return_value = version(definition.as_dict())
    return SimpleNamespace(agents=agents), definition


def test_a2a_preview_does_not_acquire_credentials_or_create_an_agent(monkeypatch, capsys):
    monkeypatch.setattr(create_prompt, "AzureCliCredential", Mock(side_effect=AssertionError))
    assert create_prompt.main(["--model-deployment", "reviewed-model", "--enable-a2a"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["agent_card"]["skills"][0]["id"] == "receiving-returnables"
    assert result["endpoint_change"]["preserve_responses_authorization_and_selector"] is True


def test_a2a_patch_preserves_responses_and_authorization_without_creating_a_version():
    before, after = a2a_agent(), a2a_agent(enabled=True)
    before["agent_endpoint"]["protocol_configuration"]["responses"] = {"configuration": {}}
    after["agent_endpoint"]["protocol_configuration"]["responses"] = {"configuration": {}}
    client, definition = a2a_client(before, after)
    result = create_prompt.enable_or_verify_a2a(client, definition)
    assert result["status"] == "enabled-a2a"
    client.agents.update_details.assert_called_once_with(
        create_prompt.AGENT_NAME,
        body={
            "agent_card": after["agent_card"],
            "agent_endpoint": {
                "protocol_configuration": after["agent_endpoint"]["protocol_configuration"],
            },
        },
    )
    assert client.agents.get.call_count == client.agents.get_version.call_count == 2
    client.agents.create_version.assert_not_called()


@pytest.mark.parametrize("reverse_protocols", [False, True])
def test_a2a_identical_configuration_is_verified_without_another_patch(reverse_protocols):
    before = a2a_agent(enabled=True)
    if reverse_protocols:
        before["agent_endpoint"]["protocols"].reverse()
    client, definition = a2a_client(before, before)
    assert create_prompt.enable_or_verify_a2a(client, definition)["status"] == "verified-existing-a2a"
    client.agents.update_details.assert_not_called()
    assert client.agents.get.call_count == 2


@pytest.mark.parametrize("change", ["card", "identity", "version", "definition", "protocols"])
def test_a2a_conflicting_preconditions_do_not_mutate_the_agent(change):
    before = a2a_agent()
    if change == "card":
        before["agent_card"] = {"description": "A different existing card"}
    elif change == "identity":
        before["agent_endpoint"]["authorization_schemes"] = [{"type": "Anonymous"}]
    elif change == "version":
        before["versions"]["latest"]["version"] = "2"
    elif change == "protocols":
        before["agent_endpoint"]["protocols"].append("activity")
    client, definition = a2a_client(before, a2a_agent(enabled=True))
    if change == "definition":
        client.agents.get_version.return_value = version({"kind": "hosted"})
    with pytest.raises(RuntimeError, match="preconditions"):
        create_prompt.enable_or_verify_a2a(client, definition)
    client.agents.update_details.assert_not_called()


def test_a2a_rejects_an_independent_read_back_with_changed_authorization():
    after = a2a_agent(enabled=True)
    after["agent_endpoint"]["authorization_schemes"] = [{"type": "Anonymous"}]
    client, definition = a2a_client(a2a_agent(), after)
    with pytest.raises(RuntimeError, match="independent A2A read-back"):
        create_prompt.enable_or_verify_a2a(client, definition)
