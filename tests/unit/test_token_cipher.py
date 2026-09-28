from cryptography.fernet import Fernet
import importlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from vitalis.config import load_settings, settings
from vitalis.domain import AuthToken
from vitalis.adapters.persistence import HealthRepository
from vitalis.adapters.persistence.database import init_db
from vitalis.adapters.persistence.models import AuthToken as StoredAuthToken
from vitalis.adapters.credentials import decrypt_token, encrypt_token


def test_vendor_token_roundtrip_is_encrypted_and_missing_key_is_refused(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "zepp_mock", False)
    monkeypatch.setattr(settings, "token_encryption_key", key)
    value = encrypt_token("synthetic-vendor-secret")
    assert value.startswith("fernet:") and "synthetic-vendor-secret" not in value
    assert decrypt_token(value) == "synthetic-vendor-secret"
    with pytest.raises(RuntimeError, match="加密格式"):
        decrypt_token("synthetic-vendor-secret")
    monkeypatch.setattr(settings, "token_encryption_key", "")
    with pytest.raises(RuntimeError, match="VITALIS_TOKEN_ENCRYPTION_KEY"):
        encrypt_token("synthetic-vendor-secret")
    with pytest.raises(RuntimeError, match="VITALIS_TOKEN_ENCRYPTION_KEY"):
        decrypt_token(value)


def test_mock_token_without_operator_key_is_opaque_and_restart_safe(monkeypatch):
    from vitalis.adapters import credentials

    monkeypatch.setattr(settings, "zepp_mock", True)
    monkeypatch.setattr(settings, "token_encryption_key", "")
    value = credentials.encrypt_token("synthetic-mock-secret")
    assert value.startswith("mock-fernet:")
    assert "synthetic-mock-secret" not in value

    # Reloading the module exercises the process-restart key derivation.
    reloaded = importlib.reload(credentials)
    assert reloaded.decrypt_token(value) == "synthetic-mock-secret"


def test_real_zepp_configuration_requires_valid_key():
    with pytest.raises(ValueError, match="VITALIS_TOKEN_ENCRYPTION_KEY"):
        load_settings({"ZEPP_MOCK": "false"})
    with pytest.raises(ValueError, match="Fernet key"):
        load_settings({"ZEPP_MOCK": "false", "VITALIS_TOKEN_ENCRYPTION_KEY": "invalid"})
    assert load_settings({
        "ZEPP_MOCK": "false",
        "VITALIS_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    }).zepp_mock is False


def test_database_never_stores_raw_vendor_token(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'tokens.db').as_posix()}")
    init_db(engine)
    try:
        with Session(engine) as db:
            with db.begin():
                HealthRepository(db).save_token(AuthToken(
                    user_id="synthetic-owner", source="zepp",
                    source_user_id="synthetic-vendor", access_token="synthetic-private-token",
                ))
        with Session(engine) as db:
            raw = db.query(StoredAuthToken).filter_by(user_id="synthetic-owner").one()
            assert raw.access_token.startswith("fernet:")
            assert "synthetic-private-token" not in raw.access_token
            assert HealthRepository(db).get_token("synthetic-owner", "zepp").access_token == "synthetic-private-token"
    finally:
        engine.dispose()


def test_mock_database_never_stores_plaintext_without_operator_key(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "zepp_mock", True)
    monkeypatch.setattr(settings, "token_encryption_key", "")
    engine = create_engine(f"sqlite:///{(tmp_path / 'mock-tokens.db').as_posix()}")
    init_db(engine)
    try:
        with Session(engine) as db:
            with db.begin():
                HealthRepository(db).save_token(AuthToken(
                    user_id="mock-owner", source="zepp",
                    source_user_id="synthetic-vendor", access_token="synthetic-private-token",
                ))
        with Session(engine) as db:
            raw = db.query(StoredAuthToken).filter_by(user_id="mock-owner").one()
            assert raw.access_token.startswith("mock-fernet:")
            assert "synthetic-private-token" not in raw.access_token
            assert HealthRepository(db).get_token("mock-owner", "zepp").access_token == "synthetic-private-token"
    finally:
        engine.dispose()
