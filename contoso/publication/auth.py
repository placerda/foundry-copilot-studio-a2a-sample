"""azd's documented external credential protocol, served on a private Unix socket."""

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socketserver
import threading
import time


# Go ARM clients append "/.default" to this trailing-slash public-cloud audience.
AUTH_PROBE_SCOPE = "https://management.core.windows.net//.default"


class BrokerRequestRejected(ValueError):
    pass


def _validate_request(request, tenant_id, scopes):
    if (not isinstance(request, dict) or set(request) - {"scopes", "tenantId"}
            or request.get("tenantId", tenant_id) not in ("", tenant_id)
            or not isinstance(request.get("scopes"), list)
            or len(request["scopes"]) != 1 or not isinstance(request["scopes"][0], str)
            or request["scopes"][0] not in scopes):
        raise BrokerRequestRejected("Unapproved token request")


def token_response(credential, request, tenant_id, scopes):
    _validate_request(request, tenant_id, scopes)
    token = credential.get_token(*request["scopes"])
    if not isinstance(token.token, str) or not token.token or token.expires_on <= time.time() + 5:
        raise ValueError("Invalid managed identity credential")
    return {
        "status": "success", "token": token.token,
        "expiresOn": datetime.fromtimestamp(token.expires_on, timezone.utc).isoformat(),
    }


class AuthEnvironment(dict):
    """Ephemeral token correlation is private; only booleans reach the caller."""

    credential_requested = False
    credential_observed = False
    credential_failed = False
    credential_rejected = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._issued = {}
        self._lock = threading.Lock()

    def record_token(self, request, result):
        digest = hashlib.sha256(result["token"].encode("utf-8")).digest()
        expiration = int(datetime.fromisoformat(result["expiresOn"]).timestamp())
        with self._lock:
            if len(self._issued) >= 16:
                self._issued.clear()
            self._issued[digest] = (expiration, request["scopes"][0])
            self.credential_observed = True

    def confirms_token(self, token, expiration, scope):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        with self._lock:
            return self._issued.get(digest) == (int(expiration), scope)

    def clear_issued(self):
        with self._lock:
            self._issued.clear()


@contextmanager
def managed_auth(workspace: Path, credential, tenant_id: str, scopes: frozenset[str]):
    if os.name != "posix":
        raise RuntimeError("Production publication requires Linux Unix-socket transport")
    directory = workspace / "identity"
    directory.mkdir(mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / "auth.sock"
    if len(str(path).encode()) > 100:
        raise ValueError("Publication workspace path exceeds Unix socket limit")
    key = secrets.token_urlsafe(32)
    observation = AuthEnvironment(AZD_AUTH_ENDPOINT="unix:" + str(path), AZD_AUTH_KEY=key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (self.path != "/token?api-version=2023-07-12-preview"
                        or not hmac.compare_digest(
                            self.headers.get("Authorization", ""), "Bearer " + key)
                        or not 0 < length <= 2048):
                    raise ValueError("Invalid credential request")
                request = json.loads(self.rfile.read(length))
                _validate_request(request, tenant_id, scopes)
            except Exception:
                observation.credential_rejected = True
                result = {
                    "status": "error", "code": "GetTokenError",
                    "message": "Credential request rejected.",
                }
                status = 403
            else:
                observation.credential_requested = True
                try:
                    result = token_response(credential, request, tenant_id, scopes)
                    observation.record_token(request, result)
                except Exception:
                    observation.credential_failed = True
                    result = {
                        "status": "error", "code": "GetTokenError",
                        "message": "Managed identity token acquisition failed.",
                    }
                # RemoteCredential decodes either envelope only after HTTP 200.
                status = 200
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(socketserver.UnixStreamServer):
        allow_reuse_address = False

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(10)
            return connection, address

    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield observation
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)
        observation.clear_issued()
