from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.logging import get_logger
from app.db.supabase_client import SupabaseClient

if TYPE_CHECKING:
    pass

logger = get_logger("db.storage")


class BaseStorage:
    """Base class for domain-specific storage services.

    Provides shared access to the Supabase client and AES-GCM encryption
    utilities for sensitive tokens.
    """

    def __init__(
        self, client: SupabaseClient | None = None, corr_id: str | None = None
    ) -> None:
        self.client = client or SupabaseClient(corr_id=corr_id)
        self.corr_id = corr_id or "system"

        # Initialize encryption
        self._aesgcm: AESGCM | None = None
        enc_key_str = settings.ENCRYPTION_KEY.get_secret_value()
        if enc_key_str:
            try:
                key = base64.urlsafe_b64decode(enc_key_str)
                if len(key) == 32:
                    self._aesgcm = AESGCM(key)
            except Exception as e:
                logger.error("Failed to initialize AES-GCM: %s", e)

    def _decrypt_token(self, token: str | None) -> str | None:
        """Decrypts a sensitive token using AES-GCM if encryption is configured.

        Args:
            token: The base64-encoded encrypted token.

        Returns:
            The plaintext token if decryption is successful, otherwise the input.

        """
        if not token or not self._aesgcm:
            return token
        try:
            data = base64.urlsafe_b64decode(token.encode())
            nonce, ct = data[:12], data[12:]
            return self._aesgcm.decrypt(nonce, ct, None).decode()
        except Exception:
            return token

    def _encrypt_token(self, token: str | None) -> str | None:
        """Encrypts a sensitive token using AES-GCM (12-byte nonce).

        Args:
            token: The plaintext token.

        Returns:
            A base64-encoded string containing the nonce and ciphertext.

        """
        import os

        if not token or not self._aesgcm:
            return token
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, token.encode(), None)
        return base64.urlsafe_b64encode(nonce + ct).decode()
