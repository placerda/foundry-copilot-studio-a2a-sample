"""Requester-only credentials in the current Windows user's protected store."""

import ctypes
import re
from ctypes import wintypes
from uuid import UUID


class CredentialUnavailable(ValueError):
    pass


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def _target(client_id):
    try:
        if str(UUID(client_id)) != client_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise CredentialUnavailable("Invalid requester identity") from None
    return "ContosoAgentDemo:requester:" + client_id


def _api():
    try:
        api = ctypes.WinDLL("advapi32", use_last_error=True)
    except (AttributeError, OSError):
        raise CredentialUnavailable("Windows Credential Manager is unavailable") from None
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _read(target):
    api = _api()
    pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise CredentialUnavailable("Requester credential is not installed")
    try:
        credential = pointer.contents
        if not 0 < credential.CredentialBlobSize <= 2560:
            raise CredentialUnavailable("Invalid requester credential")
        return credential.UserName, ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize,
        )
    finally:
        api.CredFree(pointer)


def _write(target, client_id, blob):
    api = _api()
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _Credential(
        Type=1, TargetName=target, UserName=client_id,
        Comment="Contoso local requester", CredentialBlobSize=len(blob),
        CredentialBlob=buffer, Persist=2,
    )
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialUnavailable("Requester credential could not be stored")
    finally:
        ctypes.memset(buffer, 0, len(blob))


def read_requester_secret(client_id):
    target = _target(client_id)
    username, blob = _read(target)
    try:
        secret = blob.decode("utf-8", errors="strict")
    except (ValueError, UnicodeError, AttributeError):
        raise CredentialUnavailable("Invalid requester credential") from None
    if (username != client_id or not secret.strip()
            or any(ord(char) < 32 for char in secret)):
        raise CredentialUnavailable("Invalid requester credential")
    return secret


def store_requester_secret(client_id, secret):
    target = _target(client_id)
    if (not isinstance(secret, str) or not secret.strip()
            or any(ord(char) < 32 for char in secret)):
        raise CredentialUnavailable("Invalid requester credential")
    try:
        blob = secret.encode("utf-8", errors="strict")
    except UnicodeError:
        raise CredentialUnavailable("Invalid requester credential") from None
    if len(blob) > 2560:
        raise CredentialUnavailable("Invalid requester credential")
    _write(target, client_id, blob)
    return target


def _certificate_target(client_id, fingerprint):
    target = _target(client_id)
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise CredentialUnavailable("Invalid requester certificate identity")
    return target + ":certificate:" + fingerprint


def read_requester_certificate_key(client_id, fingerprint):
    username, blob = _read(_certificate_target(client_id, fingerprint))
    if username != client_id or not isinstance(blob, bytes) or not 0 < len(blob) <= 2560:
        raise CredentialUnavailable("Invalid requester certificate credential")
    return blob


def store_requester_certificate_key(client_id, fingerprint, private_key):
    target = _certificate_target(client_id, fingerprint)
    if (not isinstance(private_key, bytes) or not 0 < len(private_key) <= 2560
            or not private_key.startswith(b"-----BEGIN PRIVATE KEY-----\n")):
        raise CredentialUnavailable("Invalid requester certificate credential")
    _write(target, client_id, private_key)
    return target
