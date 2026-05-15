"""Tests for authentication helpers in app/auth.py."""

from app.auth import (
    decode_pkce_cookie,
    decode_session_cookie,
    encode_pkce_cookie,
    encode_session_cookie,
    generate_pkce_pair,
    verify_supabase_jwt,
)


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


def test_verify_supabase_jwt_no_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "")
    assert verify_supabase_jwt("any.token.here") is None


def test_verify_supabase_jwt_invalid_token(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    assert verify_supabase_jwt("not.a.real.jwt") is None


def test_verify_supabase_jwt_valid(monkeypatch):
    from jose import jwt

    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret-key")
    payload = {"sub": "user-uuid-123", "email": "user@example.com"}
    token = jwt.encode(payload, "test-secret-key", algorithm="HS256")
    claims = verify_supabase_jwt(token)
    assert claims is not None
    assert claims["sub"] == "user-uuid-123"
