"""Loopback-only HTTP client host; no Lambda events or model runtime."""

import argparse
import json
import logging
import mimetypes
import os
import socket
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from uuid import UUID, uuid4

from .a2a_requester import A2ARequester, NATIVE_OPTIONS
from .auth import Authorizer, KeyCache
from .config import CONTOSOSettings, MAX_BODY_BYTES
from .errors import ChatFailure, invalid
from .validation import decode_json, validate_messages, validate_contoso_options

MAX_RESPONSE_BYTES = 262144
REQUEST_DEADLINE_MS = 90000
logger = logging.getLogger("contoso_local")
IDENTITY_MODES = {
    "user-delegation": {
        "enabled": False,
        "reason": "Not configured.",
    },
    "service-to-service": {
        "enabled": False,
        "reason": "Not configured.",
    },
    "shared-delegated": {
        "enabled": True,
        "reason": "Shared connection selected.",
    },
}


def capabilities():
    return {"default": "shared-delegated", "modes": IDENTITY_MODES}


@dataclass(frozen=True)
class LocalSettings:
    tenant_id: str
    audience: str
    owner_oid: str
    contoso: CONTOSOSettings
    scope: str = "access_as_user"
    max_messages: int = 10

    @property
    def issuer(self):
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        try:
            ids = [str(UUID(env[name])) for name in
                   ("TENANT_ID", "API_AUDIENCE", "ALLOWED_USER_OID")]
            return cls(*ids, CONTOSOSettings.from_env(env))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError("Configure the local Entra identity and requester credential in process environment.") from None


class ChatService:
    def __init__(self, settings, authorizer, requester):
        self.settings, self.authorizer, self.requester = settings, authorizer, requester

    def chat(self, raw, headers, request_id):
        caller = self.authorizer.verify(headers.get("authorization"))
        payload = decode_json(raw, headers, allow_identity_mode=True)
        mode = payload.pop("identity_mode", "shared-delegated")
        if not isinstance(mode, str) or mode not in IDENTITY_MODES:
            raise invalid()
        if not IDENTITY_MODES[mode]["enabled"]:
            raise ChatFailure(409, "identity_mode_unavailable", IDENTITY_MODES[mode]["reason"])
        messages = validate_messages(payload, self.settings)
        options = validate_contoso_options(payload, self.settings)
        if options != NATIVE_OPTIONS:
            raise invalid()
        state = self.requester.invoke(messages[-1]["content"], options, REQUEST_DEADLINE_MS)
        return {**state, "request_id": request_id,
                "identity": {
                    "mode": mode, "application_caller": caller,
                    "studio_caller": {"kind": "fixed-shared-user", "verified": False},
                }}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, dist, service):
        self.dist = Path(dist).resolve()
        self.service = service
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), LocalHandler)
        self.origin = f"http://localhost:{self.server_port}"

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class LocalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)
        self.read_deadline = threading.Timer(10, self._expire_read)
        self.read_deadline.daemon = True
        self.read_deadline.start()

    def _expire_read(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.read_deadline.cancel()
        super().finish()

    def log_message(self, *_):
        pass

    def _reply(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if len(body) > MAX_RESPONSE_BYTES and content_type.startswith("application/json"):
            raise invalid()
        self.send_response(status)
        for name, value in {
            "Content-Type": content_type, "Content-Length": str(len(body)),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Connection": "close", **(extra or {}),
        }.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _dispatch(self):
        request_id, start = str(uuid4()), time.monotonic()
        status, code = 500, "internal_error"
        try:
            headers = {}
            for name, value in self.headers.items():
                name = name.lower()
                if name in headers:
                    raise invalid()
                headers[name] = value
            if headers.get("host") != self.server.origin.removeprefix("http://"):
                raise ChatFailure(403, "invalid_host", "Use the configured localhost origin.")
            if self.command not in ("GET", "HEAD", "POST"):
                raise ChatFailure(405, "method_not_allowed", "Use GET or POST.")
            if self.command == "POST":
                if self.path != "/api/chat":
                    raise ChatFailure(404, "not_found", "Unsupported route.")
                if headers.get("origin") != self.server.origin:
                    raise ChatFailure(403, "invalid_origin", "Use the configured localhost origin.")
                length = headers.get("content-length", "")
                if ("transfer-encoding" in headers or not length.isdecimal()
                        or len(length) > 6 or not 0 < int(length) <= MAX_BODY_BYTES):
                    raise invalid()
                raw = self.rfile.read(int(length))
                if len(raw) != int(length):
                    raise invalid()
                self.read_deadline.cancel()
                value = self.server.service.chat(raw, headers, request_id)
                body = json.dumps(value, ensure_ascii=True).encode("utf-8")
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ChatFailure(502, "dependency_unavailable", "Invalid downstream response.")
                status, code = 200, "success"
                self._reply(status, body, extra={"X-Request-ID": request_id})
            else:
                if self.path == "/api/capabilities":
                    status, code = 200, "capabilities"
                    self._reply(200, json.dumps(capabilities()).encode())
                    return
                allowed = {
                    "/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
                    "/styles.css": "styles.css", "/config.js": "config.js",
                    "/assets/foundry.svg": "assets/foundry.svg",
                    "/assets/copilot-studio.svg": "assets/copilot-studio.svg",
                }
                if self.path not in allowed:
                    raise ChatFailure(404, "not_found", "Unsupported route.")
                path = self.server.dist / allowed[self.path]
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.server.dist):
                    raise ChatFailure(404, "not_found", "Build the frontend before starting.")
                if path.stat().st_size > 2097152:
                    raise ChatFailure(503, "unavailable", "Frontend asset too large.")
                status, code = 200, "static"
                self._reply(status, path.read_bytes(), mimetypes.guess_type(path)[0] or "application/octet-stream")
        except ChatFailure as error:
            status, code = error.status, error.code
            self._reply(status, json.dumps({"error": {"code": code, "message": error.message},
                                           "request_id": request_id}).encode(),
                        extra={"X-Request-ID": request_id,
                               **({"WWW-Authenticate": "Bearer"} if status == 401 else {}),
                               **({"Allow": "GET, HEAD, POST"} if status == 405 else {})})
        except (TimeoutError, ConnectionError, OSError):
            code = "transport_error"
            self.close_connection = True
        except Exception:
            self._reply(500, json.dumps({"error": {"code": code, "message": "Internal error."},
                                        "request_id": request_id}).encode())
        finally:
            logger.info(json.dumps({"request_id": request_id, "status": status, "code": code,
                                    "duration_ms": round((time.monotonic() - start) * 1000)}))

    do_GET = do_HEAD = do_POST = do_OPTIONS = do_PUT = do_DELETE = do_PATCH = _dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dist", default=str(Path(__file__).resolve().parents[1] / "frontend" / "dist"))
    args = parser.parse_args()
    if args.port != 8000:
        parser.error("The registered browser origin requires port 8000.")
    try:
        settings = LocalSettings.from_env()
        service = ChatService(settings, Authorizer(settings, KeyCache(settings)), A2ARequester(settings.contoso))
        with LocalServer(args.port, args.dist, service) as server:
            logging.basicConfig(level=logging.INFO)
            logger.info("Listening on http://localhost:8000/ (Ctrl+C to stop).")
            server.serve_forever()
    except (ValueError, OSError):
        parser.exit(1, "Local configuration invalid or port 8000 occupied. Preserve the existing server; configure credentials and retry.\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
