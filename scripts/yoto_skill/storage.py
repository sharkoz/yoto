from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
from typing import Any, Protocol

from .errors import YotoError

KEYCHAIN_SERVICE = "com.openai.codex.skill.yoto"
ERR_SEC_SUCCESS = 0
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300


def app_data_dir() -> Path:
    override = os.environ.get("YOTO_DATA_DIR")
    return Path(override).expanduser() if override else Path.home() / "Library/Application Support/Yoto Skill"


class ConfigStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or app_data_dir()
        self.path = self.root / "config.json"

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise YotoError("config_invalid", f"Could not read {self.path}", str(exc)) from exc

    def write(self, value: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)


class TokenStore(Protocol):
    def load(self, account: str) -> dict[str, Any] | None: ...
    def save(self, account: str, value: dict[str, Any]) -> None: ...
    def delete(self, account: str) -> None: ...


class KeychainTokenStore:
    """Store an OAuth token document in macOS Keychain, never on the command line."""

    def __init__(self, service: str = KEYCHAIN_SERVICE) -> None:
        self.service = service
        try:
            self.security = ctypes.CDLL(
                "/System/Library/Frameworks/Security.framework/Security"
            )
            self.core_foundation = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError as exc:
            raise YotoError("keychain_unavailable", "The macOS Keychain API is unavailable") from exc
        self._configure_native_signatures()

    def _configure_native_signatures(self) -> None:
        pointer = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        self.security.SecKeychainFindGenericPassword.argtypes = [
            pointer,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            ctypes.POINTER(uint32),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
        ]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.argtypes = [
            pointer,
            uint32,
            ctypes.c_char_p,
            uint32,
            ctypes.c_char_p,
            uint32,
            pointer,
            ctypes.POINTER(pointer),
        ]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [
            pointer,
            pointer,
            uint32,
            pointer,
        ]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemDelete.argtypes = [pointer]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.core_foundation.CFRelease.argtypes = [pointer]
        self.core_foundation.CFRelease.restype = None

    def _identifiers(self, account: str) -> tuple[bytes, bytes]:
        return self.service.encode(), account.encode()

    def _raise_status(self, action: str, status: int) -> None:
        raise YotoError(
            "keychain_error",
            f"macOS Keychain could not {action}",
            {"os_status": status},
        )

    def _find_item(self, account: str, include_data: bool) -> tuple[int, ctypes.c_void_p, bytes | None]:
        service, encoded_account = self._identifiers(account)
        item = ctypes.c_void_p()
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(encoded_account),
            encoded_account,
            ctypes.byref(length) if include_data else None,
            ctypes.byref(data) if include_data else None,
            ctypes.byref(item),
        )
        if status != ERR_SEC_SUCCESS:
            return status, item, None
        if not include_data:
            return status, item, None
        try:
            raw = ctypes.string_at(data, length.value)
        finally:
            self.security.SecKeychainItemFreeContent(None, data)
        return status, item, raw

    def _load_raw(self, account: str) -> bytes | None:
        status, item, raw = self._find_item(account, include_data=True)
        if status == ERR_SEC_ITEM_NOT_FOUND:
            return None
        if status != ERR_SEC_SUCCESS:
            self._raise_status("read credentials", status)
        try:
            return raw
        finally:
            if item:
                self.core_foundation.CFRelease(item)

    def _save_raw(self, account: str, raw: bytes) -> None:
        service, encoded_account = self._identifiers(account)
        buffer = ctypes.create_string_buffer(raw)
        item = ctypes.c_void_p()
        status = self.security.SecKeychainAddGenericPassword(
            None,
            len(service),
            service,
            len(encoded_account),
            encoded_account,
            len(raw),
            ctypes.cast(buffer, ctypes.c_void_p),
            ctypes.byref(item),
        )
        if status == ERR_SEC_SUCCESS:
            if item:
                self.core_foundation.CFRelease(item)
            return
        if status != ERR_SEC_DUPLICATE_ITEM:
            self._raise_status("save credentials", status)

        find_status, existing_item, _ = self._find_item(account, include_data=False)
        if find_status != ERR_SEC_SUCCESS:
            self._raise_status("find credentials for update", find_status)
        try:
            update_status = self.security.SecKeychainItemModifyAttributesAndData(
                existing_item,
                None,
                len(raw),
                ctypes.cast(buffer, ctypes.c_void_p),
            )
        finally:
            if existing_item:
                self.core_foundation.CFRelease(existing_item)
        if update_status != ERR_SEC_SUCCESS:
            self._raise_status("update credentials", update_status)

    def load(self, account: str) -> dict[str, Any] | None:
        raw = self._load_raw(account)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise YotoError("keychain_token_invalid", "Stored Yoto credentials are invalid JSON") from exc

    def save(self, account: str, value: dict[str, Any]) -> None:
        self._save_raw(account, json.dumps(value, separators=(",", ":")).encode())

    def delete(self, account: str) -> None:
        status, item, _ = self._find_item(account, include_data=False)
        if status == ERR_SEC_ITEM_NOT_FOUND:
            return
        if status != ERR_SEC_SUCCESS:
            self._raise_status("find credentials for deletion", status)
        try:
            delete_status = self.security.SecKeychainItemDelete(item)
        finally:
            if item:
                self.core_foundation.CFRelease(item)
        if delete_status != ERR_SEC_SUCCESS:
            self._raise_status("delete credentials", delete_status)
