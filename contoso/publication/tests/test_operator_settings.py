import json
from pathlib import Path

import pytest

from contoso.publication.operator_settings import publication_environment, publication_settings, publication_workspace


APIM = "11111111-1111-4111-8111-111111111111"
OLD = "22222222-2222-4222-8222-222222222222"
KEY = "CONTOSO_ROUTER_PLATFORM_CALLER_IDS"


def test_trusted_package_settings_are_project_bound_and_do_not_mutate_environment():
    original = {"unrelated": "preserved"}
    override = {"project_id": "approved", "workspace_root": str(Path.cwd()), "apim_caller_id": APIM}
    actual = publication_settings({"project_id": "APPROVED"}, original, override)
    assert actual["CONTOSO_PUBLICATION_APIM_CALLER_ID"] == APIM
    assert actual["unrelated"] == "preserved"
    assert original == {"unrelated": "preserved"}


def test_package_settings_reject_wrong_project_extra_fields_and_conflicts():
    override = {"project_id": "approved", "workspace_root": str(Path.cwd()), "apim_caller_id": APIM}
    for invalid in ({**override, "project_id": "other"}, {**override, "unexpected": "value"}):
        with pytest.raises(ValueError):
            publication_settings({"project_id": "approved"}, {}, invalid)
    with pytest.raises(ValueError):
        publication_settings(
            {"project_id": "approved"}, {"CONTOSO_PUBLICATION_APIM_CALLER_ID": OLD}, override,
        )


def test_missing_package_settings_preserve_environment():
    assert publication_settings({}, {"unrelated": "preserved"}) == {"unrelated": "preserved"}


def test_owned_workspace_and_explicit_override():
    assert publication_workspace({"HOME": str(Path.cwd())}) == Path.cwd() / "data" / "contoso-publication"
    approved = Path.cwd() / ".operator-workspaces"
    assert publication_workspace({"CONTOSO_PUBLICATION_WORKSPACE_ROOT": str(approved)}) == approved
    with pytest.raises(ValueError):
        publication_workspace({"CONTOSO_PUBLICATION_WORKSPACE_ROOT": "relative"})


def test_addition_preserves_existing_caller_and_other_bindings():
    original = {"environment_values": {KEY: json.dumps([OLD]), "unchanged": "value"}}
    actual = dict(publication_environment(original, {"CONTOSO_PUBLICATION_APIM_CALLER_ID": APIM}))
    assert json.loads(actual[KEY]) == [OLD, APIM]
    assert actual["unchanged"] == "value"
    assert json.loads(original["environment_values"][KEY]) == [OLD]


def test_no_setting_preserves_current_configuration():
    original = {"environment_values": {KEY: json.dumps([OLD])}}
    assert dict(publication_environment(original, {})) == original["environment_values"]


def test_addition_is_idempotent():
    original = {"environment_values": {KEY: json.dumps([OLD, APIM])}}
    assert json.loads(dict(publication_environment(
        original, {"CONTOSO_PUBLICATION_APIM_CALLER_ID": APIM},
    ))[KEY]) == [OLD, APIM]


@pytest.mark.parametrize("callers", [[], "not-an-array", [OLD, OLD], [""]])
def test_rejects_missing_or_invalid_existing_admission(callers):
    with pytest.raises(ValueError):
        publication_environment(
            {"environment_values": {KEY: json.dumps(callers)}},
            {"CONTOSO_PUBLICATION_APIM_CALLER_ID": APIM},
        )


def test_rejects_non_identity_and_excess_callers():
    with pytest.raises(ValueError):
        publication_environment(
            {"environment_values": {KEY: json.dumps([OLD])}},
            {"CONTOSO_PUBLICATION_APIM_CALLER_ID": "arbitrary-caller"},
        )
    with pytest.raises(ValueError):
        publication_environment(
            {"environment_values": {KEY: json.dumps([f"existing-{i}" for i in range(16)])}},
            {"CONTOSO_PUBLICATION_APIM_CALLER_ID": APIM},
        )
