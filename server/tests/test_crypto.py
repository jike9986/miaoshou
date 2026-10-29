from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from services import crypto


def test_server_envelope_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(crypto.SERVER_KEY_ENV, key)

    encrypted = crypto.encrypt_for_server("secret-value")

    assert encrypted.startswith(crypto.SERVER_PREFIX)
    assert crypto.decrypt(encrypted) == "secret-value"


def test_encrypt_uses_server_envelope_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(crypto.SERVER_KEY_ENV, key)
    monkeypatch.setattr(crypto, "_dpapi_encrypt", lambda _plain: pytest.fail("DPAPI used"))
    monkeypatch.setattr(crypto, "_fernet", lambda: pytest.fail("local key used"))

    assert crypto.encrypt("secret-value").startswith(crypto.SERVER_PREFIX)


@pytest.mark.parametrize("key", [None, ""])
def test_server_encryption_requires_configured_key(
    monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    monkeypatch.delenv(crypto.SERVER_KEY_ENV, raising=False)
    if key is not None:
        monkeypatch.setenv(crypto.SERVER_KEY_ENV, key)

    with pytest.raises(crypto.CredentialError):
        crypto.encrypt_for_server("secret-value")


def test_validate_server_key_requires_a_valid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(crypto.SERVER_KEY_ENV, raising=False)
    with pytest.raises(crypto.CredentialError):
        crypto.validate_server_key()

    monkeypatch.setenv(crypto.SERVER_KEY_ENV, Fernet.generate_key().decode("ascii"))
    assert crypto.validate_server_key() is None


def test_empty_server_credential_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(crypto.SERVER_KEY_ENV, raising=False)

    assert crypto.encrypt_for_server("") == ""


def test_invalid_server_key_does_not_fall_back_to_local_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(crypto.SERVER_KEY_ENV, "not-a-fernet-key")
    monkeypatch.setattr(crypto, "_dpapi_encrypt", lambda _plain: pytest.fail("DPAPI used"))
    monkeypatch.setattr(crypto, "_fernet", lambda: pytest.fail("local key used"))

    with pytest.raises(crypto.CredentialError):
        crypto.encrypt("secret-value")


def test_server_decryption_requires_key_and_rejects_invalid_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = crypto.SERVER_PREFIX + "token"
    monkeypatch.delenv(crypto.SERVER_KEY_ENV, raising=False)
    with pytest.raises(crypto.CredentialError, match="未配置"):
        crypto.decrypt(encrypted)

    monkeypatch.setenv(crypto.SERVER_KEY_ENV, "invalid")
    with pytest.raises(crypto.CredentialError, match="格式无效"):
        crypto.decrypt(encrypted)


def test_legacy_fernet_behavior_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.delenv(crypto.SERVER_KEY_ENV, raising=False)
    monkeypatch.setattr(crypto, "_dpapi_encrypt", lambda _plain: None)
    monkeypatch.setattr(crypto, "_dpapi_decrypt", lambda _token: None)
    monkeypatch.setattr(crypto, "_fernet", lambda: fernet)

    encrypted = crypto.encrypt("legacy-secret")

    assert not encrypted.startswith(crypto.SERVER_PREFIX)
    assert crypto.decrypt(encrypted) == "legacy-secret"


def test_decrypt_with_explicit_local_secret_avoids_settings_and_file_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    secret = "source-local-secret"
    secret_file = tmp_path / "secret.key"
    monkeypatch.delenv(crypto.SERVER_KEY_ENV, raising=False)
    monkeypatch.setattr(crypto, "_dpapi_encrypt", lambda _plain: None)
    monkeypatch.setattr(
        crypto,
        "get_settings",
        lambda: SimpleNamespace(local_secret=lambda: secret),
    )
    encrypted = crypto.encrypt("source-secret")
    assert not secret_file.exists()

    monkeypatch.setattr(
        crypto,
        "get_settings",
        lambda: pytest.fail("settings accessed during explicit-secret decryption"),
    )

    assert crypto.decrypt_with_local_secret(encrypted, secret) == "source-secret"
    assert not secret_file.exists()


def test_decrypt_with_explicit_local_secret_rejects_bad_ciphertext() -> None:
    with pytest.raises(crypto.CredentialError):
        crypto.decrypt_with_local_secret("not-a-token", "local-secret")


def test_decrypt_with_explicit_local_secret_delegates_prefixed_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv(crypto.SERVER_KEY_ENV, key)
    server_token = crypto.encrypt_for_server("server-secret")
    assert crypto.decrypt_with_local_secret(server_token, "unused") == "server-secret"

    monkeypatch.delenv(crypto.SERVER_KEY_ENV)
    monkeypatch.setattr(crypto, "_dpapi_decrypt", lambda _token: "dpapi-secret")
    assert crypto.decrypt_with_local_secret("dpapi:opaque", "unused") == "dpapi-secret"


def test_legacy_dpapi_behavior_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(crypto.SERVER_KEY_ENV, raising=False)
    monkeypatch.setattr(crypto, "_dpapi_encrypt", lambda plain: f"dpapi:{plain}")
    monkeypatch.setattr(crypto, "_dpapi_decrypt", lambda token: token.removeprefix("dpapi:"))

    encrypted = crypto.encrypt("dpapi-secret")

    assert encrypted == "dpapi:dpapi-secret"
    assert crypto.decrypt(encrypted) == "dpapi-secret"
