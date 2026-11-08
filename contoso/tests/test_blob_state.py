from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError, ResourceModifiedError, ResourceNotFoundError, ServiceRequestError,
)

from contoso.state import AzureBlobBackend, StateConflict, StateError, StateMissing, StateUnavailable


def test_read_is_bounded_and_pinned_to_the_checked_blob_version():
    blob = Mock()
    blob.get_blob_properties.return_value = SimpleNamespace(size=6, etag='"version"')
    blob.download_blob.return_value.readall.return_value = b"cipher"
    container = Mock()
    container.get_blob_client.return_value = blob
    assert AzureBlobBackend(container).read("opaque-name", 100) == (b"cipher", '"version"')
    blob.download_blob.assert_called_once_with(
        etag='"version"', match_condition=MatchConditions.IfNotModified, max_concurrency=1,
    )


def test_oversized_state_never_downloads():
    blob = Mock()
    blob.get_blob_properties.return_value = SimpleNamespace(size=101, etag='"version"')
    container = Mock()
    container.get_blob_client.return_value = blob
    with pytest.raises(StateError, match="limit"):
        AzureBlobBackend(container).read("opaque-name", 100)
    blob.download_blob.assert_not_called()


@pytest.mark.parametrize("etag", [None, '"previous-version"'])
def test_upload_uses_create_only_or_conditional_replace(etag):
    blob = Mock()
    blob.upload_blob.return_value = {"etag": '"new-version"'}
    container = Mock()
    container.get_blob_client.return_value = blob
    assert AzureBlobBackend(container).write("opaque-name", b"cipher", etag) == '"new-version"'
    args, kwargs = blob.upload_blob.call_args
    assert args == (b"cipher",)
    assert kwargs["overwrite"] is (etag is not None)
    assert kwargs["content_settings"].content_type == "application/octet-stream"
    if etag is None:
        assert "etag" not in kwargs
        assert "match_condition" not in kwargs
    else:
        assert kwargs["etag"] == etag
        assert kwargs["match_condition"] == MatchConditions.IfNotModified


def test_single_use_delete_is_conditional():
    blob = Mock()
    container = Mock()
    container.get_blob_client.return_value = blob
    AzureBlobBackend(container).delete("opaque-name", '"read-version"')
    blob.delete_blob.assert_called_once_with(
        etag='"read-version"', match_condition=MatchConditions.IfNotModified,
    )


@pytest.mark.parametrize("sdk_error,expected", [
    (ResourceNotFoundError, StateMissing),
    (ResourceExistsError, StateConflict),
    (ResourceModifiedError, StateConflict),
    (ServiceRequestError, StateUnavailable),
])
def test_sdk_failures_are_explicit_and_redacted(sdk_error, expected):
    container = Mock()
    container.get_blob_client.side_effect = sdk_error("sensitive-test-token private-detail")
    with pytest.raises(expected) as caught:
        AzureBlobBackend(container).read("opaque-name", 100)
    assert "sensitive-test-token" not in str(caught.value)
    assert "private-detail" not in str(caught.value)
