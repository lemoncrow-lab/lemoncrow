from __future__ import annotations

from urllib.parse import urlparse

from atelier.core.capabilities.team import begin_google_oidc, finish_google_oidc


def test_google_oidc_stub_round_trip() -> None:
    started = begin_google_oidc(
        "user@example.com", redirect_uri="http://localhost/callback", hosted_domain="example.com"
    )
    finished = finish_google_oidc(
        "auth-code",
        state=started["state"],
        email="user@example.com",
        hosted_domain="example.com",
    )

    authorization_url = urlparse(started["authorization_url"])
    assert authorization_url.hostname == "accounts.google.com"
    assert finished["user_id"] == "user@example.com"
    assert finished["provider"] == "google"
    assert finished["hosted_domain"] == "example.com"
