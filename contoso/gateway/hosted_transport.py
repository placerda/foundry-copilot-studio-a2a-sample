"""Server-only Hosted transport selection; the carried protocol remains A2A."""

import re
from urllib.parse import urlsplit

from contoso.router.core import _https_origin


def select_hosted_a2a_endpoint(
    native_endpoint: str, *, hosting_transport: str = "native",
) -> str:
    if hosting_transport not in ("native", "http-passthrough"):
        raise ValueError("An explicit supported Hosted A2A transport is required")
    _https_origin(native_endpoint)
    parsed = urlsplit(native_endpoint)
    if (not parsed.hostname.endswith(".services.ai.azure.com")
            or parsed.query != "api-version=v1"
            or not re.fullmatch(
                r"/api/projects/[A-Za-z0-9_-]+/agents/contoso-foundry-agent/endpoint/protocols/a2a",
                parsed.path,
            )):
        raise ValueError("An exact native Hosted A2A endpoint is required")
    if hosting_transport == "native":
        return native_endpoint
    return parsed._replace(
        path=parsed.path.removesuffix("/a2a") + "/invocations",
    ).geturl()
