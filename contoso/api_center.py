"""Authenticated, bounded API Center inventory without a local fallback."""

import asyncio
import json
import logging
import re
import threading
import time
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from azure.core.exceptions import AzureError

from contoso.bounded import run_bounded_sync
from contoso.providers import ProtocolError, ProviderTimeout, TransportError
from contoso.router.core import CatalogError


_logger = logging.getLogger(__name__)
_API_VERSION = "2024-02-01-preview"
_PATH = "/workspaces/default/apis"
_SCOPE = "https://azure-apicenter.net/.default"


class APICenterClient:
    def __init__(self, endpoint, credential, *, session_factory=requests.Session):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not re.fullmatch(
                r"[a-z0-9-]+\.data\.[a-z0-9-]+\.azure-apicenter\.ms",
                parsed.netloc,
            )
            or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment
        ):
            raise CatalogError("An administrator-configured API Center origin is required")
        self.endpoint = endpoint.rstrip("/")
        self.credential = credential
        self.session_factory = session_factory
        self._slot = threading.BoundedSemaphore(1)

    def _url(self, value):
        target = urljoin(self.endpoint + _PATH, value)
        parsed = urlsplit(target)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            f"{parsed.scheme}://{parsed.netloc}" != self.endpoint
            or parsed.path != _PATH or parsed.fragment or len(target) > 4096
            or query.get("api-version") != [_API_VERSION]
        ):
            raise ProtocolError("API Center returned an unapproved pagination URL")
        return target

    def _list(self):
        deadline = time.monotonic() + 15
        token = self.credential.get_token(_SCOPE)
        if (
            not isinstance(token.token, str) or not token.token
            or len(token.token) > 16384
            or any(c.isspace() or ord(c) < 32 for c in token.token)
            or token.expires_on <= time.time() + 15
        ):
            raise TransportError("The API Center service credential is unavailable")
        url = self.endpoint + _PATH + f"?api-version={_API_VERSION}&maxpagesize=32"
        records, visited = [], set()
        with self.session_factory() as session:
            session.trust_env = False
            for _ in range(4):
                url = self._url(url)
                if url in visited:
                    raise ProtocolError("API Center pagination contains a cycle")
                visited.add(url)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderTimeout("API Center inventory exceeded its time budget")
                with session.get(
                    url,
                    headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"},
                    timeout=(min(3, remaining), min(5, remaining)),
                    allow_redirects=False, stream=True,
                ) as response:
                    if response.status_code != 200 or response.history:
                        raise TransportError("The authenticated API Center inventory is unavailable")
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=4096):
                        if time.monotonic() >= deadline:
                            raise ProviderTimeout("API Center inventory exceeded its time budget")
                        body.extend(chunk)
                        if len(body) > 131072:
                            raise ProtocolError("API Center inventory exceeded its byte limit")
                try:
                    page = json.loads(body, object_pairs_hook=_unique_fields)
                except (ValueError, UnicodeError, RecursionError):
                    raise ProtocolError("API Center returned invalid inventory JSON") from None
                if (
                    not isinstance(page, dict) or not isinstance(page.get("value"), list)
                    or any(not isinstance(record, dict) for record in page["value"])
                    or len(records) + len(page["value"]) > 128
                ):
                    raise ProtocolError("API Center returned an invalid inventory page")
                records.extend(page["value"])
                next_link = page.get("nextLink")
                if next_link is None:
                    return records
                if not isinstance(next_link, str) or not next_link:
                    raise ProtocolError("API Center returned an invalid pagination link")
                url = next_link
        raise ProtocolError("API Center inventory exceeded its page limit")

    async def list_apis(self):
        try:
            async with asyncio.timeout(15):
                return await run_bounded_sync(self._slot, self._list)
        except (TimeoutError, requests.Timeout):
            _logger.warning("service=api_center outcome=timeout")
            raise ProviderTimeout("API Center inventory exceeded its time budget") from None
        except (AzureError, requests.RequestException):
            _logger.warning("service=api_center outcome=credential_or_transport_failure")
            raise TransportError("The authenticated API Center inventory is unavailable") from None
        except (ProtocolError, TransportError, ProviderTimeout):
            _logger.warning("service=api_center outcome=inventory_rejected")
            raise


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("API Center returned duplicate JSON fields")
        result[key] = value
    return result
