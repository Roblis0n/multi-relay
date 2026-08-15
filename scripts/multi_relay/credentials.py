"""Operating-system credential storage with no file or argv secret transport."""

from __future__ import annotations

import getpass
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .errors import ManagerError


CREDENTIAL_TARGET = "codex-deepseek-api-key"
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def credential_target(provider_id: str, protocol: str | None = None) -> str:
    """Return the vault target for one provider without exposing a secret."""

    if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
        raise ManagerError("catalog_invalid", "Provider identifier is invalid.")
    if provider_id == "deepseek":
        return CREDENTIAL_TARGET
    return f"codex-multi-relay-{provider_id}-api-key"


class CredentialStore(Protocol):
    def exists(self) -> bool: ...

    def store(self, secret: str) -> None: ...

    def read(self) -> str | None: ...

    def remove(self) -> bool: ...


def _validate_secret(secret: str, protocol: str) -> None:
    if not isinstance(secret, str) or not secret or any(
        character in secret for character in "\r\n\0"
    ):
        raise ManagerError("invalid_api_key", "Enter a valid provider credential.")
    if protocol == "deepseek-chat" and not secret.startswith("sk-"):
        raise ManagerError("invalid_api_key", "Enter a valid DeepSeek API Key.")


def _selected_protocol(provider_id: str, protocol: str | None) -> str:
    if protocol is not None:
        return protocol
    if provider_id == "deepseek":
        return "deepseek-chat"
    raise ManagerError(
        "catalog_invalid",
        "Custom providers require an explicit protocol for credential handling.",
        {"provider": provider_id},
    )


class _Win32CredentialApi:
    """Small protected wrapper over the Windows Credential Manager API."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class CredentialW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(CredentialW)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredWriteW.argtypes = [ctypes.POINTER(CredentialW), wintypes.DWORD]
        advapi32.CredWriteW.restype = wintypes.BOOL
        advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        advapi32.CredDeleteW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        self.ctypes = ctypes
        self.credential_type = CredentialW
        self.advapi32 = advapi32

    def read(self, target: str) -> str | None:
        ctypes = self.ctypes
        credential = ctypes.POINTER(self.credential_type)()
        if not self.advapi32.CredReadW(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise OSError(error, "Credential Manager read failed")
        try:
            raw = ctypes.string_at(
                credential.contents.CredentialBlob,
                credential.contents.CredentialBlobSize,
            )
            return raw.decode("utf-8")
        finally:
            self.advapi32.CredFree(credential)

    def write(self, target: str, account: str, secret: str) -> None:
        ctypes = self.ctypes
        raw = secret.encode("utf-8")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = self.credential_type()
        credential.Flags = 0
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = account
        if not self.advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise OSError(ctypes.get_last_error(), "Credential Manager write failed")

    def delete(self, target: str) -> bool:
        if self.advapi32.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0):
            return True
        error = self.ctypes.get_last_error()
        if error == self.ERROR_NOT_FOUND:
            return False
        raise OSError(error, "Credential Manager delete failed")


class WindowsCredentialStore:
    def __init__(
        self,
        api: object | None = None,
        account: str | None = None,
        *,
        target: str = CREDENTIAL_TARGET,
        protocol: str = "deepseek-chat",
    ) -> None:
        self._api = api or _Win32CredentialApi()
        self._account = account or getpass.getuser()
        self._target = target
        self._protocol = protocol

    def read(self) -> str | None:
        try:
            return self._api.read(self._target)  # type: ignore[attr-defined]
        except Exception:
            raise ManagerError(
                "credential_read_failed",
                "Windows Credential Manager could not read the provider credential.",
            ) from None

    def exists(self) -> bool:
        return self.read() is not None

    def store(self, secret: str) -> None:
        _validate_secret(secret, self._protocol)
        try:
            self._api.write(self._target, self._account, secret)  # type: ignore[attr-defined]
        except Exception:
            raise ManagerError(
                "credential_write_failed",
                "Windows Credential Manager could not store the provider credential.",
            ) from None

    def remove(self) -> bool:
        try:
            return bool(self._api.delete(self._target))  # type: ignore[attr-defined]
        except Exception:
            raise ManagerError(
                "credential_delete_failed",
                "Windows Credential Manager could not remove the provider credential.",
            ) from None


class _MacOSKeychainApi:
    """Direct Security.framework binding; secrets never enter process arguments."""

    ERR_SEC_ITEM_NOT_FOUND = -25300

    def __init__(self) -> None:
        import ctypes

        security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        uint32 = ctypes.c_uint32
        void_pointer = ctypes.c_void_p
        security.SecKeychainFindGenericPassword.argtypes = [
            void_pointer,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            ctypes.POINTER(uint32),
            ctypes.POINTER(void_pointer),
            ctypes.POINTER(void_pointer),
        ]
        security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        security.SecKeychainAddGenericPassword.argtypes = [
            void_pointer,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            uint32,
            void_pointer,
            ctypes.POINTER(void_pointer),
        ]
        security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        security.SecKeychainItemModifyAttributesAndData.argtypes = [
            void_pointer,
            void_pointer,
            uint32,
            void_pointer,
        ]
        security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        security.SecKeychainItemDelete.argtypes = [void_pointer]
        security.SecKeychainItemDelete.restype = ctypes.c_int32
        security.SecKeychainItemFreeContent.argtypes = [void_pointer, void_pointer]
        security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        core_foundation.CFRelease.argtypes = [void_pointer]
        core_foundation.CFRelease.restype = None
        self.ctypes = ctypes
        self.security = security
        self.core_foundation = core_foundation

    @staticmethod
    def _identity(target: str, account: str) -> tuple[bytes, bytes]:
        return target.encode("utf-8"), account.encode("utf-8")

    def _find_item(self, target: str, account: str) -> tuple[int, object]:
        ctypes = self.ctypes
        service, user = self._identity(target, account)
        item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(user),
            user,
            None,
            None,
            ctypes.byref(item),
        )
        return int(status), item

    def read(self, target: str, account: str) -> str | None:
        ctypes = self.ctypes
        service, user = self._identity(target, account)
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = int(
            self.security.SecKeychainFindGenericPassword(
                None,
                len(service),
                service,
                len(user),
                user,
                ctypes.byref(length),
                ctypes.byref(data),
                None,
            )
        )
        if status == self.ERR_SEC_ITEM_NOT_FOUND:
            return None
        if status != 0:
            raise OSError(status, "Keychain read failed")
        try:
            return ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            self.security.SecKeychainItemFreeContent(None, data)

    def write(self, target: str, account: str, secret: str) -> None:
        ctypes = self.ctypes
        service, user = self._identity(target, account)
        raw = secret.encode("utf-8")
        buffer = ctypes.create_string_buffer(raw)
        status, item = self._find_item(target, account)
        if status == 0:
            try:
                updated = self.security.SecKeychainItemModifyAttributesAndData(
                    item,
                    None,
                    len(raw),
                    ctypes.cast(buffer, ctypes.c_void_p),
                )
            finally:
                self.core_foundation.CFRelease(item)
            if updated != 0:
                raise OSError(int(updated), "Keychain update failed")
            return
        if status != self.ERR_SEC_ITEM_NOT_FOUND:
            raise OSError(status, "Keychain lookup failed")
        added = self.security.SecKeychainAddGenericPassword(
            None,
            len(service),
            service,
            len(user),
            user,
            len(raw),
            ctypes.cast(buffer, ctypes.c_void_p),
            None,
        )
        if added != 0:
            raise OSError(int(added), "Keychain write failed")

    def delete(self, target: str, account: str) -> bool:
        status, item = self._find_item(target, account)
        if status == self.ERR_SEC_ITEM_NOT_FOUND:
            return False
        if status != 0:
            raise OSError(status, "Keychain lookup failed")
        try:
            deleted = self.security.SecKeychainItemDelete(item)
        finally:
            self.core_foundation.CFRelease(item)
        if deleted != 0:
            raise OSError(int(deleted), "Keychain delete failed")
        return True


class MacOSCredentialStore:
    def __init__(
        self,
        api: object | None = None,
        account: str | None = None,
        *,
        target: str = CREDENTIAL_TARGET,
        protocol: str = "deepseek-chat",
    ) -> None:
        self._api = api or _MacOSKeychainApi()
        self._account = account or getpass.getuser()
        self._target = target
        self._protocol = protocol

    def read(self) -> str | None:
        try:
            return self._api.read(self._target, self._account)  # type: ignore[attr-defined]
        except Exception:
            raise ManagerError(
                "credential_read_failed",
                "macOS Keychain could not read the provider credential.",
            ) from None

    def exists(self) -> bool:
        return self.read() is not None

    def store(self, secret: str) -> None:
        _validate_secret(secret, self._protocol)
        try:
            self._api.write(self._target, self._account, secret)  # type: ignore[attr-defined]
        except Exception:
            raise ManagerError(
                "credential_write_failed",
                "macOS Keychain could not store the provider credential.",
            ) from None

    def remove(self) -> bool:
        try:
            return bool(self._api.delete(self._target, self._account))  # type: ignore[attr-defined]
        except Exception:
            raise ManagerError(
                "credential_delete_failed",
                "macOS Keychain could not remove the provider credential.",
            ) from None


def credential_store(
    platform: str | None = None,
    *,
    provider_id: str = "deepseek",
    protocol: str | None = None,
) -> CredentialStore:
    """Return the current platform's protected credential store."""

    selected_protocol = _selected_protocol(provider_id, protocol)
    target = credential_target(provider_id, selected_protocol)
    selected = platform or ("windows" if os.name == "nt" else sys.platform)
    if selected in {"windows", "win32"}:
        return WindowsCredentialStore(target=target, protocol=selected_protocol)
    if selected in {"darwin", "macos"}:
        return MacOSCredentialStore(target=target, protocol=selected_protocol)
    raise ManagerError(
        "unsupported_platform",
        "Multi Relay credential storage supports Windows and macOS only.",
    )


def provider_auth_command(
    provider_id: str = "deepseek",
    codex_home: Path | None = None,
    start_bridge: bool = True,
    *,
    protocol: str | None = None,
) -> list[str]:
    """Return a stable command that prints the protected key to Codex only."""

    helper = Path(__file__).with_name("credential_helper.py").resolve()
    selected_protocol = _selected_protocol(provider_id, protocol)
    command = [
        sys.executable,
        str(helper),
        "--provider",
        provider_id,
        "--protocol",
        selected_protocol,
    ]
    if not start_bridge:
        command.append("--no-start-bridge")
    if codex_home is not None:
        command.extend(["--codex-home", str(codex_home)])
    return command


def prompt_and_store(
    store: CredentialStore,
    prompt_fn: Callable[[str], str] = getpass.getpass,
) -> None:
    """Prompt locally with masking and store without echoing the credential."""

    secret = prompt_fn("Provider API credential (stored in the operating-system credential vault): ")
    store.store(secret)
