import http.client
import json
import time
import threading
from concurrent.futures import Future
from urllib.parse import urlsplit

import jwt

if __package__:
    from .errors import ChatFailure, dependency, unauthenticated
else:
    from errors import ChatFailure, dependency, unauthenticated

TTL = 300
COOLDOWN = 30
DEADLINE = 4
MAX_DOCUMENT_BYTES = 262144
_transport_slot = threading.BoundedSemaphore(1)


def trusted_url(url):
    if not isinstance(url, str):
        raise ValueError
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "login.microsoftonline.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment):
        raise ValueError
    return parsed


def fetch_document(url, timeout):
    trusted_url(url)
    deadline = time.monotonic() + timeout
    if timeout <= 0 or not _transport_slot.acquire(blocking=False):
        raise TimeoutError("Identity transport unavailable within deadline")
    result = Future()

    def run():
        try:
            document = _fetch_document(url, deadline - time.monotonic())
        except Exception as error:
            # Propagate worker failures; never substitute an identity document.
            _transport_slot.release()
            result.set_exception(error)
        else:
            _transport_slot.release()
            result.set_result(document)

    # DNS and HTTP headers can outlive socket timeouts. One daemon worker bounds
    # caller latency without accumulating workers if a transport remains stuck.
    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _transport_slot.release()
        raise
    return result.result(timeout=max(0, deadline - time.monotonic()))


def _fetch_document(url, timeout):
    if timeout <= 0:
        raise TimeoutError
    parsed = trusted_url(url)
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
    try:
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Identity HTTP failure")
        length = response.getheader("Content-Length")
        if length and int(length) > MAX_DOCUMENT_BYTES:
            raise ValueError
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if connection.sock:
                connection.sock.settimeout(remaining)
            # read1 avoids waiting for a full block from a slow streaming peer.
            chunk = response.read1(min(16384, MAX_DOCUMENT_BYTES + 1 - len(raw)))
            raw.extend(chunk)
            if len(raw) > MAX_DOCUMENT_BYTES:
                raise ValueError
            if not chunk:
                break
        if time.monotonic() > deadline:
            raise TimeoutError
        return json.loads(raw.decode("utf-8"))
    finally:
        connection.close()


class KeyCache:
    def __init__(self, settings, http=None, clock=None):
        self.settings = settings
        self.http = http or fetch_document
        self.clock = clock or time.monotonic
        self.keys = {}
        self.expires = 0
        self.last_attempt = float("-inf")
        self.last_failed = False

    def refresh(self):
        start = self.clock()
        self.last_attempt = start
        self.last_failed = True
        try:
            discovery = self.http(
                f"https://login.microsoftonline.com/{self.settings.tenant_id}/v2.0/.well-known/openid-configuration",
                DEADLINE)
            if not isinstance(discovery, dict) or discovery.get("issuer") != self.settings.issuer:
                raise ValueError
            url = discovery.get("jwks_uri")
            trusted_url(url)
            remaining = DEADLINE - (self.clock() - start)
            if remaining <= 0:
                raise TimeoutError
            document = self.http(url, remaining)
            if self.clock() - start > DEADLINE:
                raise TimeoutError
            if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                raise ValueError
            keys = {}
            seen = set()
            for key in document["keys"]:
                if not isinstance(key, dict):
                    raise ValueError
                kid = key.get("kid")
                if not isinstance(kid, str) or not kid or kid in seen:
                    raise ValueError
                seen.add(kid)
                if key.get("use", "sig") != "sig":
                    continue
                if (key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256"
                        or any(name in key for name in ("d", "p", "q", "dp", "dq", "qi", "oth"))
                        or key.get("key_ops", ["verify"]) != ["verify"]):
                    raise ValueError
                keys[kid] = jwt.algorithms.RSAAlgorithm.from_jwk(key)
            if not keys:
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError, jwt.PyJWTError, http.client.HTTPException):
            raise dependency() from None
        self.keys = keys
        self.expires = self.clock() + TTL
        self.last_failed = False

    def get(self, kid):
        if not isinstance(kid, str) or not kid or len(kid) > 256:
            raise unauthenticated()
        if self.clock() < self.expires and kid in self.keys:
            return self.keys[kid]
        if self.clock() - self.last_attempt < COOLDOWN:
            if self.last_failed or self.clock() >= self.expires:
                raise dependency()
            raise unauthenticated()
        self.refresh()
        if kid not in self.keys:
            raise unauthenticated()
        return self.keys[kid]


class Authorizer:
    def __init__(self, settings, cache):
        self.settings = settings
        self.cache = cache

    def verify(self, header):
        if not isinstance(header, str) or len(header) > 16384:
            raise unauthenticated()
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise unauthenticated()
        token = parts[1]
        try:
            header_data = jwt.get_unverified_header(token)
            if header_data.get("alg") != "RS256":
                raise unauthenticated()
            key = self.cache.get(header_data.get("kid"))
            claims = jwt.decode(token, key, algorithms=["RS256"],
                                audience=self.settings.audience, issuer=self.settings.issuer,
                                leeway=0, options={"require": ["exp", "iss", "aud", "tid", "ver"],
                                                  "strict_aud": True})
            if (claims["ver"] != "2.0" or claims["tid"] != self.settings.tenant_id
                    or type(claims["exp"]) not in (int, float)
                    or ("nbf" in claims and type(claims["nbf"]) not in (int, float))):
                raise unauthenticated()
        except (jwt.PyJWTError, ValueError, TypeError):
            raise unauthenticated() from None
        scope = claims.get("scp")
        if (not isinstance(scope, str) or self.settings.scope not in scope.split()
                or claims.get("oid") != self.settings.owner_oid):
            raise ChatFailure(403, "forbidden", "This user does not have access to the chat.")
