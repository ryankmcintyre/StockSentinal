"""Test helpers for submitting valid CSRF-protected forms."""

from app.csrf import CSRF_COOKIE_NAME, CSRF_FIELD_NAME, create_csrf_token


def csrf_form_data(client, data: dict | None = None) -> dict:
    token = create_csrf_token()
    client.cookies.set(CSRF_COOKIE_NAME, token)
    form_data = dict(data or {})
    form_data[CSRF_FIELD_NAME] = token
    return form_data
