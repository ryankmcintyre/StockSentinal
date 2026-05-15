"""Tests for authentication helpers in app/auth.py."""

import base64

import pytest

from app import auth as auth_module
from app.auth import (
    decode_pkce_cookie,
    decode_session_cookie,
    encode_pkce_cookie,
    encode_session_cookie,
    generate_pkce_pair,
    verify_supabase_jwt,
)


@pytest.fixture(autouse=True)
def clear_jwks_cache():
    auth_module._jwks_cache.clear()
    yield
    auth_module._jwks_cache.clear()


def test_session_cookie_roundtrip():
    user_id = "abc-123"
    cookie = encode_session_cookie(user_id)
    assert decode_session_cookie(cookie) == user_id


def test_session_cookie_invalid():
    assert decode_session_cookie("not-a-valid-cookie") is None


def test_pkce_cookie_roundtrip():
    verifier = "my-verifier"
    cookie = encode_pkce_cookie(verifier)
    assert decode_pkce_cookie(cookie) == verifier


def test_pkce_cookie_invalid():
    assert decode_pkce_cookie("bad-cookie") is None


def test_generate_pkce_pair():
    verifier, challenge = generate_pkce_pair()
    assert len(verifier) > 32
    assert len(challenge) > 32
    assert "=" not in challenge


def test_verify_supabase_jwt_missing_supabase_url(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    valid_header_only_token = "eyJhbGciOiJSUzI1NiIsImtpZCI6InRlc3Qta2V5In0.e30.signature"
    assert verify_supabase_jwt(valid_header_only_token) is None


def test_verify_supabase_jwt_invalid_token(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    assert verify_supabase_jwt("not.a.real.jwt") is None


def test_verify_supabase_jwt_rejects_unsupported_algorithm(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    unsupported_alg_token = "eyJhbGciOiJIUzI1NiIsImtpZCI6InRlc3Qta2V5In0.e30.signature"
    assert verify_supabase_jwt(unsupported_alg_token) is None


def test_verify_supabase_jwt_valid_via_jwks(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwt

    def b64url_uint(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
        "n": b64url_uint(public_numbers.n),
        "e": b64url_uint(public_numbers.e),
    }

    token = jwt.encode(
        {"sub": "user-uuid-123", "email": "user@example.com"},
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    class MockResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [jwk]}

    monkeypatch.setattr("app.auth.httpx.get", lambda *args, **kwargs: MockResponse())

    claims = verify_supabase_jwt(token)
    assert claims is not None
    assert claims["sub"] == "user-uuid-123"


def test_verify_supabase_jwt_rejects_non_signing_jwk(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwt

    def b64url_uint(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key",
        "use": "enc",
        "alg": "RS256",
        "n": b64url_uint(public_numbers.n),
        "e": b64url_uint(public_numbers.e),
    }
    token = jwt.encode(
        {"sub": "user-uuid-123", "email": "user@example.com"},
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    class MockResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [jwk]}

    monkeypatch.setattr("app.auth.httpx.get", lambda *args, **kwargs: MockResponse())

    assert verify_supabase_jwt(token) is None
