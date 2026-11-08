from collections.abc import Callable
import logging
import time
from urllib.parse import urlsplit

import azure.functions as func

from contoso.connections import DelegatedConnectionBroker
from contoso.identity import EntraAuthenticator

from .http import Gateway, NetworkDependency
from .settings import GatewaySettings


def create_function_app(
    settings: GatewaySettings, *, broker: DelegatedConnectionBroker,
    network_client: NetworkDependency, authenticator: EntraAuthenticator | None = None,
    logger: logging.Logger | None = None, clock: Callable[[], float] = time.time,
    enable_invoke: bool = True,
    a2a_bridge=None,
    enable_unknown_route: bool = True,
) -> func.FunctionApp:
    """Parent composition supplies dependencies and exports the returned app.

    Functions keys are not JWT authentication. The handler verifies every
    non-callback route; Easy Auth must exclude only /api/oauth/callback.
    Requires the Functions host's default /api route prefix.
    Disable the wildcard when composing additional routes to avoid shadowing them.
    """
    gateway = Gateway(
        settings, broker=broker, network_client=network_client,
        authenticator=authenticator, logger=logger, clock=clock,
        enable_invoke=enable_invoke,
        a2a_bridge=a2a_bridge,
    )
    app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

    async def dispatch(request: func.HttpRequest) -> func.HttpResponse:
        try:
            parsed = urlsplit(request.url)
            path = parsed.path if not parsed.query and not parsed.fragment else "/invalid-query"
            response = await gateway.handle(
                request.method, path, headers=request.headers, body=request.get_body(),
            )
        except Exception:
            # Request-adapter failures also go through the static, no-store error surface.
            response = await gateway.handle("POST", "/unknown", headers={}, body=b"")
        return func.HttpResponse(
            body=response.body, status_code=response.status_code, headers=dict(response.headers),
        )

    for name, route in (
        ("contoso_invoke", "invoke"), ("contoso_prepare", "connections/prepare"),
        ("contoso_status", "connections/status"),
        ("contoso_token", "connections/token"), ("contoso_callback", "oauth/callback"),
    ):
        if name == "contoso_invoke" and not enable_invoke:
            continue
        app.function_name(name=name)(app.route(
            route=route, methods=["POST"], trigger_arg_name="request",
        )(dispatch))
    if a2a_bridge is not None:
        app.function_name(name="contoso_a2a")(app.route(
            route="a2a", methods=["POST"], trigger_arg_name="request",
        )(dispatch))
    if enable_unknown_route:
        app.function_name(name="contoso_unknown")(app.route(
            route="{*unmatched}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            trigger_arg_name="request",
        )(dispatch))
    return app
