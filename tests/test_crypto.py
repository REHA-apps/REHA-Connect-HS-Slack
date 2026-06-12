import pytest  # noqa: D100, F401, I001
from app.db.storage_service import StorageService


def test_aes_gcm_nonce_uniqueness():
    """
    Test to satisfy security review item 6.3:
    Assert that encrypting the same plaintext twice with the same key
    produces different ciphertexts (due to unique os.urandom(12) nonces).
    """  # noqa: D212
    # Create a dummy storage service to access encryption methods
    storage = StorageService(corr_id="test-crypto")

    plaintext = "super_secret_token_123"

    # Encrypt the same plaintext twice
    ciphertext1 = storage._encrypt_token(plaintext)
    ciphertext2 = storage._encrypt_token(plaintext)

    # The ciphertexts MUST be different because the 12-byte GCM nonce must be unique per encryption  # noqa: E501
    assert ciphertext1 != ciphertext2, (
        "AES-GCM encryption produced identical ciphertexts, nonce reuse detected!"
    )  # noqa: E501

    # But they must both decrypt to the same original plaintext
    assert storage._decrypt_token(ciphertext1) == plaintext
    assert storage._decrypt_token(ciphertext2) == plaintext
