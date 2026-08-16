"""Operating-system credential storage with no file or argv secret transport."""

from __future__ import annotations

import getpass
import hmac
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .errors import ManagerError


CREDENTIAL_TARGET = "codex-deepseek-api-key"
"""Legacy DeepSeek target. New writes must use :class:`VaultLocator`."""

VAULT_APPLICATION = "multi-relay"
LOCAL_GATEWAY_PROVIDER_ID = "local-gateway"
LOCAL_GATEWAY_CREDENTIAL_ID = "session"
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CREDENTIAL_ID = _PROVIDER_ID


@dataclass(frozen=True)
class VaultLocator:
    """One canonical, provider-scoped location in an operating-system vault."""

    provider_id: str
    credential_id: str
    _target_override: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not _PROVIDER_ID.fullmatch(
            self.provider_id
        ):
            raise ManagerError("catalog_invalid", "Provider identifier is invalid.")
        if not isinstance(self.credential_id, str) or not _CREDENTIAL_ID.fullmatch(
            self.credential_id
        ):
            raise ManagerError("catalog_invalid", "Credential identifier is invalid.")
        if self._target_override is not None and (
            not isinstance(self._target_override, str)
            or not self._target_override
            or any(character in self._target_override for character in "\r\n\0")
        ):
            raise ManagerError("catalog_invalid", "Vault target is invalid.")

    @classmethod
    def from_target(
        cls,
        provider_id: str,
        credential_id: str,
        vault_target: str,
    ) -> "VaultLocator":
        canonical = cls(provider_id, credential_id)
        if vault_target == canonical.target:
            return canonical
        return cls(provider_id, credential_id, vault_target)

    @property
    def canonical_target(self) -> str:
        return f"{VAULT_APPLICATION}/{self.provider_id}/{self.credential_id}"

    @property
    def target(self) -> str:
        return self._target_override or self.canonical_target

    @property
    def is_canonical(self) -> bool:
        return self._target_override is None

    @property
    def macos_service(self) -> str:
        return VAULT_APPLICATION if self.is_canonical else self.target

    @property
    def macos_account(self) -> str | None:
        return f"{self.provider_id}/{self.credential_id}" if self.is_canonical else None

    @property
    def linux_attributes(self) -> tuple[str, ...]:
        if not self.is_canonical:
            raise ManagerError(
                "credential_migration_required",
                "The Linux credential reference must be migrated to the canonical vault target.",
                {
                    "provider": self.provider_id,
                    "credential": self.credential_id,
                },
            )
        return (
            "application",
            VAULT_APPLICATION,
            "provider",
            self.provider_id,
            "credential",
            self.credential_id,
        )


def credential_target(
    provider_id: str,
    credential_id: str = "primary",
    *,
    protocol: str | None = None,
) -> str:
    """Return the canonical non-secret target for one provider credential."""

    if protocol is not None:
        _selected_protocol(provider_id, protocol)
    return VaultLocator(provider_id, credential_id).target


def legacy_credential_target(provider_id: str) -> str:
    """Return a former provider-level target for migration-only compatibility."""

    VaultLocator(provider_id, "primary")
    if provider_id == "deepseek":
        return CREDENTIAL_TARGET
    return f"codex-multi-relay-{provider_id}-api-key"


class CredentialStore(Protocol):
    def exists(self) -> bool: ...

    def store(self, secret: str) -> None: ...

    def read(self) -> str | None: ...

    def remove(self) -> bool: ...


class CredentialReference(Protocol):
    id: str
    provider_id: str
    vault_target: str
    enabled: bool
    created_at: str
    label: str


def redact_secret(text: object, *secrets: str | None) -> str:
    """Return a safe diagnostic string with known in-memory secrets removed."""

    redacted = str(text)
    for secret in sorted(
        {value for value in secrets if isinstance(value, str) and value},
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redacted_error(
    code: str,
    message: str,
    *secrets: str | None,
    details: dict[str, object] | None = None,
) -> ManagerError:
    """Build a structured error only after a final known-secret redaction pass."""

    return ManagerError(code, redact_secret(message, *secrets), details)


class _LegacyMigrationCredentialStore:
    """Expose the legacy DeepSeek slot only for one-time read and cleanup."""

    def __init__(self, store: CredentialStore) -> None:
        self._store = store

    def exists(self) -> bool:
        return self._store.exists()

    def read(self) -> str | None:
        return self._store.read()

    def store(self, secret: str) -> None:
        del secret
        raise ManagerError(
            "legacy_credential_read_only",
            "The legacy DeepSeek credential target is read-only and can only be migrated.",
        )

    def remove(self) -> bool:
        return self._store.remove()


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
        locator: VaultLocator | None = None,
        target: str | None = None,
        protocol: str = "deepseek-chat",
    ) -> None:
        self._api = api or _Win32CredentialApi()
        self._account = account or getpass.getuser()
        if locator is not None and target is not None:
            raise ManagerError(
                "catalog_invalid",
                "Specify a vault locator or a legacy target, not both.",
            )
        self._locator = locator or VaultLocator(
            "deepseek",
            "primary",
            target,
        )
        self._target = self._locator.target
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
            raise _redacted_error(
                "credential_write_failed",
                "Windows Credential Manager could not store the provider credential.",
                secret,
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
        locator: VaultLocator | None = None,
        target: str | None = None,
        protocol: str = "deepseek-chat",
    ) -> None:
        self._api = api or _MacOSKeychainApi()
        if locator is not None and target is not None:
            raise ManagerError(
                "catalog_invalid",
                "Specify a vault locator or a legacy target, not both.",
            )
        self._locator = locator or VaultLocator(
            "deepseek",
            "primary",
            target,
        )
        self._target = self._locator.macos_service
        self._account = account or self._locator.macos_account or getpass.getuser()
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
            raise _redacted_error(
                "credential_write_failed",
                "macOS Keychain could not store the provider credential.",
                secret,
            ) from None

    def remove(self) -> bool:
        try:
            return bool(self._api.delete(self._target, self._account))  # type: ignore[attr-defined]
        except Exception:
            raise ManagerError(
                "credential_delete_failed",
                "macOS Keychain could not remove the provider credential.",
            ) from None


class LinuxSecretServiceCredentialStore:
    """Secret Service backend implemented through secret-tool with stdin writes."""

    def __init__(
        self,
        *,
        locator: VaultLocator,
        protocol: str,
        label: str | None = None,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._locator = locator
        self._protocol = protocol
        self._label = label or f"Multi Relay: {locator.provider_id}/{locator.credential_id}"
        if (
            not isinstance(self._label, str)
            or not self._label
            or any(character in self._label for character in "\r\n\0")
        ):
            raise ManagerError("catalog_invalid", "Credential label is invalid.")
        self._runner = runner

    @property
    def _attributes(self) -> tuple[str, ...]:
        return self._locator.linux_attributes

    def _run(
        self,
        command: list[str],
        *,
        secret: str | None = None,
    ) -> Any:
        try:
            return self._runner(
                command,
                input=secret.encode("utf-8") if secret is not None else None,
                capture_output=True,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, OSError):
            raise _redacted_error(
                "credential_backend_unavailable",
                "Linux Secret Service is unavailable; install secret-tool and unlock a Secret Service collection.",
                secret,
            ) from None
        except Exception:
            raise _redacted_error(
                "credential_backend_failed",
                "Linux Secret Service could not run the credential operation.",
                secret,
            ) from None

    def read(self) -> str | None:
        result = self._run(["secret-tool", "lookup", *self._attributes])
        if result.returncode == 1 and not result.stderr:
            return None
        if result.returncode != 0:
            raise ManagerError(
                "credential_read_failed",
                "Linux Secret Service could not read the provider credential.",
            )
        raw = bytes(result.stdout)
        if raw.endswith(b"\n"):
            raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ManagerError(
                "credential_read_failed",
                "Linux Secret Service returned an invalid provider credential.",
            ) from None
        return value or None

    def exists(self) -> bool:
        return self.read() is not None

    def store(self, secret: str) -> None:
        _validate_secret(secret, self._protocol)
        result = self._run(
            [
                "secret-tool",
                "store",
                f"--label={self._label}",
                *self._attributes,
            ],
            secret=secret,
        )
        if result.returncode != 0:
            raise _redacted_error(
                "credential_write_failed",
                "Linux Secret Service could not store the provider credential.",
                secret,
            )

    def remove(self) -> bool:
        result = self._run(["secret-tool", "clear", *self._attributes])
        if result.returncode == 1 and not result.stderr:
            return False
        if result.returncode != 0:
            raise ManagerError(
                "credential_delete_failed",
                "Linux Secret Service could not remove the provider credential.",
            )
        return True


def credential_store(
    platform: str | None = None,
    *,
    provider_id: str = "deepseek",
    credential_id: str = "primary",
    protocol: str | None = None,
    vault_target: str | None = None,
    label: str | None = None,
) -> CredentialStore:
    """Return the current platform's protected credential store."""

    selected_protocol = _selected_protocol(provider_id, protocol)
    locator = (
        VaultLocator.from_target(provider_id, credential_id, vault_target)
        if vault_target is not None
        else VaultLocator(provider_id, credential_id)
    )
    selected = platform or ("windows" if os.name == "nt" else sys.platform)
    store: CredentialStore
    if selected in {"windows", "win32"}:
        store = WindowsCredentialStore(locator=locator, protocol=selected_protocol)
    elif selected in {"darwin", "macos"}:
        store = MacOSCredentialStore(locator=locator, protocol=selected_protocol)
    elif selected == "linux" or selected.startswith("linux"):
        store = LinuxSecretServiceCredentialStore(
            locator=locator,
            protocol=selected_protocol,
            label=label,
        )
    else:
        raise ManagerError(
            "unsupported_platform",
            "Multi Relay credential storage supports Windows, macOS, and Linux Secret Service.",
        )
    if vault_target == CREDENTIAL_TARGET:
        return _LegacyMigrationCredentialStore(store)
    return store


def local_gateway_credential_store(platform: str | None = None) -> CredentialStore:
    """Return the independent vault slot containing the active gateway token."""

    return credential_store(
        platform,
        provider_id=LOCAL_GATEWAY_PROVIDER_ID,
        credential_id=LOCAL_GATEWAY_CREDENTIAL_ID,
        protocol="local-gateway",
        label="Multi Relay local gateway session",
    )


def credential_metadata(
    references: Iterable[CredentialReference],
    *,
    provider_id: str | None = None,
    store_factory: Callable[..., CredentialStore] | None = None,
) -> list[dict[str, object]]:
    """List non-secret catalog metadata without touching any vault backend."""

    del store_factory
    selected: list[dict[str, object]] = []
    for reference in references:
        if provider_id is not None and reference.provider_id != provider_id:
            continue
        selected.append(
            {
                "id": reference.id,
                "provider_id": reference.provider_id,
                "vault_target": reference.vault_target,
                "enabled": reference.enabled,
                "created_at": reference.created_at,
                "label": reference.label,
            }
        )
    return selected


def read_credential_for_execution(
    reference: CredentialReference,
    *,
    protocol: str,
    platform: str | None = None,
    store_factory: Callable[..., CredentialStore] = credential_store,
) -> str:
    """Resolve an enabled reference and return its secret only to execution code."""

    if not reference.enabled:
        raise ManagerError(
            "credential_disabled",
            "The selected credential is disabled.",
            {
                "provider": reference.provider_id,
                "credential": reference.id,
            },
        )
    try:
        store = store_factory(
            platform,
            provider_id=reference.provider_id,
            credential_id=reference.id,
            protocol=protocol,
            vault_target=reference.vault_target,
        )
        secret = store.read()
    except ManagerError:
        raise
    except Exception:
        raise ManagerError(
            "credential_read_failed",
            "The operating-system vault could not read the selected credential.",
            {
                "provider": reference.provider_id,
                "credential": reference.id,
            },
        ) from None
    if not secret:
        raise ManagerError(
            "credential_missing",
            "The selected credential is not present in the operating-system vault.",
            {
                "provider": reference.provider_id,
                "credential": reference.id,
            },
        )
    return secret


@dataclass(frozen=True)
class LegacyCredentialMigrationResult:
    migrated: bool
    verified: bool
    legacy_removed: bool
    cleanup_pending: bool
    destination_target: str
    cleanup_error: str | None = None


def migrate_legacy_deepseek_credential(
    *,
    source_store: CredentialStore | None = None,
    destination_store: CredentialStore | None = None,
    switch_reference: Callable[[str], None] | None = None,
    platform: str | None = None,
) -> LegacyCredentialMigrationResult:
    """Copy, verify, switch, then clean up the one legacy DeepSeek vault slot."""

    destination_target = VaultLocator("deepseek", "primary").target
    selected_platform = platform or ("windows" if os.name == "nt" else sys.platform)
    if source_store is None and (
        selected_platform == "linux" or selected_platform.startswith("linux")
    ):
        return LegacyCredentialMigrationResult(
            migrated=False,
            verified=False,
            legacy_removed=False,
            cleanup_pending=False,
            destination_target=destination_target,
        )
    source = source_store or credential_store(
        platform,
        provider_id="deepseek",
        credential_id="primary",
        protocol="deepseek-chat",
        vault_target=CREDENTIAL_TARGET,
    )
    destination = destination_store or credential_store(
        platform,
        provider_id="deepseek",
        credential_id="primary",
        protocol="deepseek-chat",
    )
    switch = switch_reference or (lambda target: None)
    try:
        secret = source.read()
    except Exception:
        raise ManagerError(
            "credential_migration_failed",
            "The legacy DeepSeek credential could not be read for migration.",
        ) from None
    if not secret:
        return LegacyCredentialMigrationResult(
            migrated=False,
            verified=False,
            legacy_removed=False,
            cleanup_pending=False,
            destination_target=destination_target,
        )

    created_destination = False
    try:
        existing = destination.read()
        if existing is not None and not hmac.compare_digest(existing, secret):
            raise ManagerError(
                "credential_migration_conflict",
                "The canonical DeepSeek credential target already contains a different credential.",
            )
        if existing is None:
            destination.store(secret)
            created_destination = True
        verified = destination.read()
        if verified is None or not hmac.compare_digest(verified, secret):
            raise ManagerError(
                "credential_migration_verification_failed",
                "The migrated DeepSeek credential could not be verified.",
            )
        switch(destination_target)
    except Exception as error:
        rollback_failed = False
        if created_destination:
            try:
                destination.remove()
            except Exception:
                rollback_failed = True
        if (
            isinstance(error, ManagerError)
            and error.code == "credential_migration_conflict"
            and not rollback_failed
        ):
            raise ManagerError(
                error.code,
                "The canonical DeepSeek credential target already contains a different credential.",
            ) from None
        raise ManagerError(
            "credential_migration_rollback_failed"
            if rollback_failed
            else "credential_migration_failed",
            "The legacy DeepSeek credential migration failed and the original reference was preserved."
            if not rollback_failed
            else "The legacy DeepSeek credential migration failed and its destination could not be rolled back.",
        ) from None

    try:
        removed = source.remove()
    except Exception:
        removed = False
    return LegacyCredentialMigrationResult(
        migrated=True,
        verified=True,
        legacy_removed=removed,
        cleanup_pending=not removed,
        destination_target=destination_target,
        cleanup_error=None if removed else "credential_delete_failed",
    )


def provider_auth_command(
    provider_id: str = "deepseek",
    codex_home: Path | None = None,
    start_bridge: bool = True,
    *,
    credential_id: str = "primary",
    protocol: str | None = None,
    vault_target: str | None = None,
) -> list[str]:
    """Return a stable provider auth command without placing a key in argv."""

    helper = Path(__file__).with_name("credential_helper.py").resolve()
    selected_protocol = _selected_protocol(provider_id, protocol)
    locator = (
        VaultLocator.from_target(provider_id, credential_id, vault_target)
        if vault_target is not None
        else VaultLocator(provider_id, credential_id)
    )
    command = [
        sys.executable,
        str(helper),
        "--provider",
        provider_id,
        "--credential",
        credential_id,
        "--protocol",
        selected_protocol,
    ]
    if vault_target is not None:
        command.extend(["--vault-target", locator.target])
    if not start_bridge:
        command.append("--no-start-bridge")
    if codex_home is not None:
        command.extend(["--codex-home", str(codex_home)])
    return command


def prompt_and_store(
    store: CredentialStore,
    *,
    credential_label: str | None = None,
    prompt_fn: Callable[[str], str] = getpass.getpass,
) -> None:
    """Prompt locally with masking and store without echoing the credential."""

    label = credential_label or "Provider"
    if not isinstance(label, str) or not label or any(
        character in label for character in "\r\n\0"
    ):
        raise ManagerError("catalog_invalid", "Credential label is invalid.")
    secret = prompt_fn(
        f"{label} API credential (stored in the operating-system credential vault): "
    )
    store.store(secret)
