"""Per-Windows-user Kortex credentials in Windows Credential Manager.

The credential is never saved in the application settings or an episode.
No plaintext fallback is used if Windows Credential Manager is unavailable.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes


class CredentialStoreError(RuntimeError):
    """Windows could not safely read, save or delete the credential."""


def credential_target(ip: str, username: str) -> str:
    identity = (ip.strip().lower() + '\0' + username.strip()).encode('utf-8')
    return 'JARVIS-Robot-Console/Kortex/' + hashlib.sha256(identity).hexdigest()


class _Credential(ctypes.Structure):
    _fields_ = [
        ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Use generic credentials in the current Windows login profile."""

    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _NOT_FOUND = 1168
    # CRED_MAX_CREDENTIAL_BLOB_SIZE is 5*512 bytes.
    _MAX_BLOB = 2560

    def __init__(self):
        if os.name != 'nt':
            raise CredentialStoreError('Windows Credential Manager is unavailable')
        try:
            self._api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
        except OSError as error:
            raise CredentialStoreError('Windows Credential Manager is unavailable') from error
        self._api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.POINTER(ctypes.POINTER(_Credential))]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    def load(self, ip: str, username: str) -> str | None:
        result = ctypes.POINTER(_Credential)()
        if not self._api.CredReadW(credential_target(ip, username), self._TYPE_GENERIC, 0,
                                   ctypes.byref(result)):
            error = ctypes.get_last_error()
            if error == self._NOT_FOUND:
                return None
            raise CredentialStoreError(f'Credential Manager read failed (Windows {error})')
        try:
            credential = result.contents
            blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            try:
                return blob.decode('utf-8')
            except UnicodeDecodeError as error:
                raise CredentialStoreError('Stored credential is unreadable') from error
        finally:
            self._api.CredFree(result)

    def save(self, ip: str, username: str, password: str) -> None:
        secret = password.encode('utf-8')
        if not secret or len(secret) > self._MAX_BLOB:
            raise CredentialStoreError('Password is empty or exceeds Windows credential limit')
        buffer = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
        credential = _Credential()
        credential.Type = self._TYPE_GENERIC
        credential.TargetName = credential_target(ip, username)
        credential.CredentialBlobSize = len(secret)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self._PERSIST_LOCAL_MACHINE
        credential.UserName = username.strip()
        try:
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialStoreError(f'Credential Manager write failed (Windows {ctypes.get_last_error()})')
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def delete(self, ip: str, username: str) -> None:
        if not self._api.CredDeleteW(credential_target(ip, username), self._TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != self._NOT_FOUND:
                raise CredentialStoreError(f'Credential Manager delete failed (Windows {error})')
