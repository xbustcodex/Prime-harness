from __future__ import annotations

import base64
import binascii
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from prime_harness.domain import ConnectionRecord, SecretMetadata, SecretRecord, new_id, now_utc
from prime_harness.stores import Store

_MAGIC = b"PHSE1"
_NONCE_SIZE = 12
_KEY_SIZE = 32
_PURPOSE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_Result = TypeVar("_Result")


class SecretKeyProvider(Protocol):
    def get_key(self) -> bytes: ...


class EnvironmentSecretKeyProvider:
    """Loads a base64-encoded 256-bit key from the process environment."""

    def __init__(self, variable: str = "PRIME_HARNESS_SECRET_KEY") -> None:
        self.variable = variable

    def get_key(self) -> bytes:
        encoded = os.environ.get(self.variable)
        if encoded is None:
            raise SecretOperationError("Secret key material is unavailable")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise SecretOperationError("Secret key material is invalid") from None
        if len(key) != _KEY_SIZE:
            raise SecretOperationError("Secret key material is invalid")
        return key


class SecretOperationError(RuntimeError):
    """A safe-to-report secret operation failure."""


@dataclass(slots=True)
class SecretService:
    store: Store
    key_provider: SecretKeyProvider
    audit: Callable[[str, str, str, dict[str, str], str, str], object]
    now: Callable[[], datetime] = now_utc

    def create_secret(
        self, connection_id: str, purpose: str, plaintext: str
    ) -> SecretMetadata:
        connection = self._connection(connection_id, permission="manage_secrets")
        if not _PURPOSE_PATTERN.fullmatch(purpose):
            raise SecretOperationError("Secret purpose is invalid")
        if not isinstance(plaintext, str) or not plaintext:
            raise SecretOperationError("Secret value is invalid")

        secret_id = new_id("secret")
        try:
            record = SecretRecord(
                secret_id=secret_id,
                lane_id=connection.lane_id,
                owner_id=connection.owner_id,
                purpose=purpose,
                encrypted_value=self._encrypt(
                    secret_id, connection.lane_id, connection.owner_id, purpose, plaintext
                ),
                created_at=self.now(),
            )
            self.store.create_secret(record)
        except Exception:
            self._audit_operation(
                connection.lane_id,
                secret_id,
                purpose,
                "secret_created",
                "failed",
                connection_id,
            )
            raise SecretOperationError("Secret creation failed") from None
        self._audit_operation(
            connection.lane_id,
            secret_id,
            purpose,
            "secret_created",
            "succeeded",
            connection_id,
        )
        return self._metadata(record)

    def update_secret(
        self, connection_id: str, secret_id: str, plaintext: str
    ) -> SecretMetadata:
        record = self._owned_secret(connection_id, secret_id, "manage_secrets")
        if not isinstance(plaintext, str) or not plaintext:
            raise SecretOperationError("Secret value is invalid")
        try:
            record.encrypted_value = self._encrypt(
                record.secret_id,
                record.lane_id,
                record.owner_id,
                record.purpose,
                plaintext,
            )
            record.rotated_at = self.now()
            self.store.create_secret(record)
        except Exception:
            self._audit_operation(
                record.lane_id,
                secret_id,
                record.purpose,
                "secret_updated",
                "failed",
                connection_id,
            )
            raise SecretOperationError("Secret update failed") from None
        self._audit_operation(
            record.lane_id,
            secret_id,
            record.purpose,
            "secret_updated",
            "succeeded",
            connection_id,
        )
        return self._metadata(record)

    def use_secret(
        self,
        connection_id: str,
        secret_id: str,
        operation: Callable[[str], _Result],
    ) -> _Result:
        record = self._owned_secret(connection_id, secret_id, "use_secret")
        try:
            plaintext = self._decrypt(record)
            result = operation(plaintext)
        except Exception:
            self._audit_operation(
                record.lane_id,
                secret_id,
                record.purpose,
                "secret_used",
                "failed",
                connection_id,
            )
            raise SecretOperationError("Secret use failed") from None
        self._audit_operation(
            record.lane_id,
            secret_id,
            record.purpose,
            "secret_used",
            "succeeded",
            connection_id,
        )
        return result

    def delete_secret(self, connection_id: str, secret_id: str) -> bool:
        record = self._owned_secret(connection_id, secret_id, "manage_secrets")
        try:
            deleted = self.store.delete_secret(secret_id)
        except Exception:
            self._audit_operation(
                record.lane_id,
                secret_id,
                record.purpose,
                "secret_deleted",
                "failed",
                connection_id,
            )
            raise SecretOperationError("Secret deletion failed") from None
        self._audit_operation(
            record.lane_id,
            secret_id,
            record.purpose,
            "secret_deleted",
            "succeeded" if deleted else "failed",
            connection_id,
        )
        return deleted

    def list_secrets(self, connection_id: str, lane_id: str) -> list[SecretMetadata]:
        connection = self._connection(connection_id, permission="read")
        if connection.lane_id != lane_id:
            raise SecretOperationError("Secret access denied")
        return [
            item
            for item in self.store.list_secrets(lane_id)
            if item.owner_id == connection.owner_id
        ]

    def _connection(self, connection_id: str, permission: str) -> ConnectionRecord:
        connection = self.store.get_connection(connection_id)
        if (
            connection is None
            or connection.revoked_at is not None
            or (connection.expires_at is not None and connection.expires_at <= self.now())
            or permission not in connection.permissions
        ):
            raise SecretOperationError("Secret access denied")
        return connection

    def _owned_secret(self, connection_id: str, secret_id: str, permission: str) -> SecretRecord:
        record = self.store.get_secret(secret_id)
        if record is None:
            raise SecretOperationError("Secret access denied")
        try:
            connection = self._connection(connection_id, permission)
        except SecretOperationError:
            self._audit_operation(
                record.lane_id,
                secret_id,
                record.purpose,
                "secret_access_denied",
                "denied",
                connection_id,
            )
            raise
        if connection.lane_id != record.lane_id or connection.owner_id != record.owner_id:
            self._audit_operation(
                record.lane_id,
                secret_id,
                record.purpose,
                "secret_access_denied",
                "denied",
                connection_id,
            )
            raise SecretOperationError("Secret access denied")
        return record

    def _encrypt(
        self, secret_id: str, lane_id: str, owner_id: str, purpose: str, plaintext: str
    ) -> bytes:
        try:
            key = self._key()
            nonce = os.urandom(_NONCE_SIZE)
            associated_data = self._associated_data(secret_id, lane_id, owner_id, purpose)
            ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), associated_data)
        except Exception:
            raise SecretOperationError("Secret encryption failed") from None
        return _MAGIC + nonce + ciphertext

    def _decrypt(self, record: SecretRecord) -> str:
        encrypted = record.encrypted_value
        if not encrypted.startswith(_MAGIC) or len(encrypted) < len(_MAGIC) + _NONCE_SIZE + 16:
            raise SecretOperationError("Secret ciphertext is invalid")
        try:
            key = self._key()
            start = len(_MAGIC)
            nonce = encrypted[start : start + _NONCE_SIZE]
            associated_data = self._associated_data(
                record.secret_id, record.lane_id, record.owner_id, record.purpose
            )
            plaintext = AESGCM(key).decrypt(
                nonce, encrypted[start + _NONCE_SIZE :], associated_data
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            raise SecretOperationError("Secret decryption failed") from None
        except SecretOperationError:
            raise
        except Exception:
            raise SecretOperationError("Secret decryption failed") from None

    def _key(self) -> bytes:
        try:
            key = self.key_provider.get_key()
        except Exception:
            raise SecretOperationError("Secret key material is unavailable or invalid") from None
        if not isinstance(key, bytes) or len(key) != _KEY_SIZE:
            raise SecretOperationError("Secret key material is unavailable or invalid")
        return key

    @staticmethod
    def _associated_data(secret_id: str, lane_id: str, owner_id: str, purpose: str) -> bytes:
        return "\0".join((secret_id, lane_id, owner_id, purpose)).encode("utf-8")

    def _audit_operation(
        self,
        lane_id: str,
        secret_id: str,
        purpose: str,
        event_type: str,
        status: str,
        actor_id: str,
    ) -> None:
        self.audit(
            lane_id,
            event_type,
            "security",
            {"secret_id": secret_id, "purpose": purpose, "status": status},
            actor_id,
            secret_id,
        )

    @staticmethod
    def _metadata(record: SecretRecord) -> SecretMetadata:
        return SecretMetadata(
            secret_id=record.secret_id,
            lane_id=record.lane_id,
            owner_id=record.owner_id,
            purpose=record.purpose,
            created_at=record.created_at,
            rotated_at=record.rotated_at,
            revoked_at=record.revoked_at,
        )
