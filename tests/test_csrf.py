"""Tests for CSRF protection on state-changing form routes."""

from fastapi.testclient import TestClient

from app.csrf import CSRF_COOKIE_NAME, CSRF_FIELD_NAME, create_csrf_token
from app.main import app
from tests.csrf_utils import csrf_form_data


def test_post_with_valid_csrf_token_is_allowed():
    client = TestClient(app, follow_redirects=False)

    resp = client.post("/auth/logout", data=csrf_form_data(client))

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_post_without_csrf_token_is_rejected():
    client = TestClient(app, follow_redirects=False)

    resp = client.post("/auth/logout")

    assert resp.status_code == 403


def test_post_with_mismatched_csrf_token_is_rejected():
    client = TestClient(app, follow_redirects=False)
    cookie_token = create_csrf_token()
    form_token = create_csrf_token()
    client.cookies.set(CSRF_COOKIE_NAME, cookie_token)

    resp = client.post("/auth/logout", data={CSRF_FIELD_NAME: form_token})

    assert resp.status_code == 403
