from collections.abc import Callable
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

from azure.identity import ManagedIdentityCredential
from azure.storage.blob import ContainerClient

from contoso.state import AzureBlobBackend, EncryptedStateStore


def create_managed_identity_store(
    *, container_url: str, encoded_key: str, namespace: str,
    managed_identity_client_id: str | None = None,
    clock: Callable[[], float] = time.time,
) -> EncryptedStateStore:
    """Private Blob DNS/access controls and key secret references are deployment work."""
    try:
        parsed = urlsplit(container_url)
        if (not isinstance(container_url, str) or parsed.scheme != "https"
                or not parsed.hostname or not parsed.hostname.endswith(".blob.core.windows.net")
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment
                or not re.fullmatch(r"/[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]", parsed.path)
                or "--" in parsed.path or "\\" in container_url
                or any(c.isspace() or ord(c) < 32 for c in container_url)):
            raise ValueError
        if managed_identity_client_id is not None:
            managed_identity_client_id = str(UUID(managed_identity_client_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Gateway state requires an HTTPS Azure Blob container and managed identity") from None
    credential = ManagedIdentityCredential(client_id=managed_identity_client_id)
    container = ContainerClient.from_container_url(
        container_url, credential=credential,
        connection_timeout=3.05, read_timeout=10, retry_total=1,
    )
    return EncryptedStateStore(
        AzureBlobBackend(container), encoded_key, namespace=namespace, clock=clock,
    )
