"""OIDC Authorization Code + PKCE and complete human-role boundary tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from conftest import ManifestFactory, settings_for
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

import kitsune_workspace.security as security_module
from kitsune_workspace.app import create_app
from kitsune_workspace.config import WorkspaceSettings
from kitsune_workspace.security import FixedWindowRateLimiter


def _oidc_configuration(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("KITSUNE_TEST_OIDC_CLIENT_SECRET", "provider-client-secret")
    monkeypatch.setenv(
        "KITSUNE_TEST_OIDC_SESSION_SECRET",
        "workspace-session-secret-with-at-least-32-bytes",
    )
    return {
        "mode": "oidc",
        "issuer": "https://issuer.example.invalid",
        "client_id": "kitsune-workspace",
        "client_secret": "env://KITSUNE_TEST_OIDC_CLIENT_SECRET",
        "redirect_uri": "https://workspace.example.test/api/auth/callback",
        "session_secret": "env://KITSUNE_TEST_OIDC_SESSION_SECRET",
        "role_claim": "workspace_roles",
        "default_role": "viewer",
    }


def test_oidc_rejects_short_resolved_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHORT_SESSION_SECRET", "short")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    with pytest.raises(ValueError, match="at least 32 bytes"):
        WorkspaceSettings.model_validate(
            {
                "auth": {
                    "mode": "oidc",
                    "issuer": "https://issuer.example.invalid",
                    "client_id": "workspace",
                    "client_secret": "env://OIDC_CLIENT_SECRET",
                    "redirect_uri": "https://workspace.example.test/api/auth/callback",
                    "session_secret": "env://SHORT_SESSION_SECRET",
                }
            }
        )


def test_oidc_requires_explicit_redirect_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """OIDC startup cannot derive its callback from an untrusted request Host."""

    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "OIDC_SESSION_SECRET",
        "workspace-session-secret-with-at-least-32-bytes",
    )
    with pytest.raises(ValueError, match="redirect_uri"):
        WorkspaceSettings.model_validate(
            {
                "auth": {
                    "mode": "oidc",
                    "issuer": "https://issuer.example.invalid",
                    "client_id": "workspace",
                    "client_secret": "env://OIDC_CLIENT_SECRET",
                    "session_secret": "env://OIDC_SESSION_SECRET",
                }
            }
        )


@pytest.mark.parametrize("reference_kind", ["env", "file"])
def test_oidc_rejects_empty_resolved_client_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reference_kind: str,
) -> None:
    """OIDC cannot start with an empty environment or file credential."""

    monkeypatch.setenv(
        "OIDC_SESSION_SECRET",
        "workspace-session-secret-with-at-least-32-bytes",
    )
    if reference_kind == "env":
        monkeypatch.setenv("OIDC_CLIENT_SECRET", " \t")
        client_secret = "env://OIDC_CLIENT_SECRET"  # noqa: S105 - reference, not a secret
    else:
        secret_file = tmp_path / "empty-oidc-secret"
        secret_file.write_text("\r\n", encoding="utf-8")
        client_secret = f"file://{secret_file}"

    with pytest.raises(ValueError, match="is empty"):
        WorkspaceSettings.model_validate(
            {
                "auth": {
                    "mode": "oidc",
                    "issuer": "https://issuer.example.invalid",
                    "client_id": "workspace",
                    "client_secret": client_secret,
                    "redirect_uri": "https://workspace.example.test/api/auth/callback",
                    "session_secret": "env://OIDC_SESSION_SECRET",
                }
            }
        )


def test_oidc_discovery_callback_jwks_pkce_and_secure_session(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_factory()
    settings = settings_for(tmp_path, auth=_oidc_configuration(monkeypatch))
    application = create_app(settings)
    discovery = {
        "authorization_endpoint": "https://issuer.example.invalid/authorize",
        "token_endpoint": "https://issuer.example.invalid/token",
        "jwks_uri": "https://issuer.example.invalid/jwks",
    }
    application.state.oidc._discovery = discovery
    with TestClient(application, base_url="https://workspace.example.test") as client:
        login = client.get("/api/auth/login?return_to=/agents/demo-agent", follow_redirects=False)
        assert login.status_code == 302
        assert login.headers["Cache-Control"] == "no-store"
        assert login.headers["Pragma"] == "no-cache"
        authorization_query = parse_qs(urlparse(login.headers["location"]).query)
        assert authorization_query["response_type"] == ["code"]
        assert authorization_query["code_challenge_method"] == ["S256"]
        assert authorization_query["nonce"]
        assert authorization_query["state"]
        assert authorization_query["redirect_uri"] == [settings.auth.redirect_uri]
        transient_token = client.cookies.get("kitsune_oidc_state")
        assert transient_token is not None
        transient = application.state.oidc.decode(transient_token)
        assert transient["state"] == authorization_query["state"][0]
        assert transient["nonce"] == authorization_query["nonce"][0]

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
        public_jwk.update({"kid": "provider-key", "alg": "RS256", "use": "sig"})
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": settings.auth.issuer,
                "aud": [settings.auth.client_id, "kitsune-secondary-audience"],
                "azp": settings.auth.client_id,
                "sub": "oidc-user",
                "name": "OIDC User",
                "workspace_roles": ["admin"],
                "nonce": transient["nonce"],
                "iat": now,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "provider-key"},
        )
        exchanges: list[dict[str, Any]] = []

        class ProviderClient:
            def __init__(self, **_: Any) -> None:
                self.options = _

            async def __aenter__(self) -> ProviderClient:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                return None

            async def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
                assert url == discovery["token_endpoint"]
                exchanges.append(data)
                return httpx.Response(
                    200,
                    json={"id_token": id_token},
                    request=httpx.Request("POST", url),
                )

            async def get(self, url: str) -> httpx.Response:
                assert url == discovery["jwks_uri"]
                return httpx.Response(
                    200,
                    json={"keys": [public_jwk]},
                    request=httpx.Request("GET", url),
                )

        monkeypatch.setattr(security_module.httpx, "AsyncClient", ProviderClient)
        mismatch = client.get(
            "/api/auth/callback?code=authorization-code&state=wrong",
            follow_redirects=False,
        )
        assert mismatch.status_code == 400
        assert mismatch.headers["Cache-Control"] == "no-store"
        callback = client.get(
            f"/api/auth/callback?code=authorization-code&state={transient['state']}",
            follow_redirects=False,
        )
        assert callback.status_code == 302
        assert callback.headers["Cache-Control"] == "no-store"
        assert callback.headers["location"] == "/agents/demo-agent"
        assert exchanges[0]["code_verifier"] == transient["verifier"]
        assert exchanges[0]["redirect_uri"] == settings.auth.redirect_uri
        expected_client_secret = settings.auth.client_secret
        assert expected_client_secret is not None
        assert exchanges[0]["client_secret"] == expected_client_secret.get_secret_value()
        session_cookie = next(
            value
            for value in callback.headers.get_list("set-cookie")
            if value.startswith("kitsune_session=")
        )
        assert "HttpOnly" in session_cookie
        assert "Secure" in session_cookie
        assert "SameSite=lax" in session_cookie
        principal = client.get("/api/auth/me")
        assert principal.status_code == 200
        assert principal.headers["Cache-Control"] == "no-store"
        assert principal.json()["subject"] == "oidc-user"
        assert principal.json()["roles"] == ["admin"]
        assert principal.json()["csrf_token"]
        logout = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": principal.json()["csrf_token"]},
        )
        assert logout.status_code == 204
        assert logout.headers["Cache-Control"] == "no-store"


def test_oidc_uses_configured_redirect_uri_with_hostile_request_host(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request Host cannot influence authorization or token-exchange callback URIs."""

    manifest_factory()
    settings = settings_for(tmp_path, auth=_oidc_configuration(monkeypatch))
    application = create_app(settings)
    discovery = {
        "authorization_endpoint": "https://issuer.example.invalid/authorize",
        "token_endpoint": "https://issuer.example.invalid/token",
        "jwks_uri": "https://issuer.example.invalid/jwks",
    }
    application.state.oidc._discovery = discovery
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "provider-key", "alg": "RS256", "use": "sig"})
    application.state.oidc._jwks = {"keys": [public_jwk]}
    exchanges: list[dict[str, Any]] = []
    with TestClient(application, base_url="https://workspace.example.test") as client:
        login = client.get(
            "/api/auth/login",
            headers={"Host": "attacker.example"},
            follow_redirects=False,
        )
        query = parse_qs(urlparse(login.headers["location"]).query)
        assert query["redirect_uri"] == [settings.auth.redirect_uri]
        transient_token = client.cookies.get("kitsune_oidc_state")
        assert transient_token is not None
        transient = application.state.oidc.decode(transient_token)
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": settings.auth.issuer,
                "aud": settings.auth.client_id,
                "sub": "oidc-user",
                "nonce": transient["nonce"],
                "iat": now,
                "exp": now + 300,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "provider-key"},
        )

        class ProviderClient:
            def __init__(self, **_: Any) -> None:
                return None

            async def __aenter__(self) -> ProviderClient:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                return None

            async def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
                exchanges.append(data)
                return httpx.Response(
                    200,
                    json={"id_token": id_token},
                    request=httpx.Request("POST", url),
                )

        monkeypatch.setattr(security_module.httpx, "AsyncClient", ProviderClient)
        callback = client.get(
            f"/api/auth/callback?code=authorization-code&state={transient['state']}",
            headers={"Host": "attacker.example"},
            follow_redirects=False,
        )

    assert callback.status_code == 302
    assert exchanges[0]["redirect_uri"] == settings.auth.redirect_uri


@pytest.mark.parametrize(
    ("audience", "authorized_party"),
    [
        (["kitsune-workspace", "secondary-audience"], None),
        ("kitsune-workspace", "other-client"),
    ],
)
def test_oidc_rejects_missing_or_mismatched_authorized_party(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    audience: str | list[str],
    authorized_party: str | None,
) -> None:
    """Multiple audiences require azp and every present azp must match this client."""

    manifest_factory()
    settings = settings_for(tmp_path, auth=_oidc_configuration(monkeypatch))
    application = create_app(settings)
    discovery = {
        "authorization_endpoint": "https://issuer.example.invalid/authorize",
        "token_endpoint": "https://issuer.example.invalid/token",
        "jwks_uri": "https://issuer.example.invalid/jwks",
    }
    application.state.oidc._discovery = discovery
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "provider-key", "alg": "RS256", "use": "sig"})
    application.state.oidc._jwks = {"keys": [public_jwk]}

    with TestClient(application, base_url="https://workspace.example.test") as client:
        assert client.get("/api/auth/login", follow_redirects=False).status_code == 302
        transient_token = client.cookies.get("kitsune_oidc_state")
        assert transient_token is not None
        transient = application.state.oidc.decode(transient_token)
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": settings.auth.issuer,
            "aud": audience,
            "sub": "oidc-user",
            "nonce": transient["nonce"],
            "iat": now,
            "exp": now + 300,
        }
        if authorized_party is not None:
            claims["azp"] = authorized_party
        id_token = jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "provider-key"},
        )

        class ProviderClient:
            def __init__(self, **_: Any) -> None:
                return None

            async def __aenter__(self) -> ProviderClient:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                return None

            async def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={"id_token": id_token},
                    request=httpx.Request("POST", url),
                )

        monkeypatch.setattr(security_module.httpx, "AsyncClient", ProviderClient)
        callback = client.get(
            f"/api/auth/callback?code=authorization-code&state={transient['state']}",
            follow_redirects=False,
        )

    assert callback.status_code == 401
    assert callback.headers["Cache-Control"] == "no-store"
    assert client.cookies.get("kitsune_session") is None


@pytest.mark.parametrize(
    "return_to",
    [
        "/\\evil.example",
        "//evil.example/path",
        "/%5Cevil.example",
        "/%2F%2Fevil.example",
        "/line%0Abreak",
    ],
)
def test_oidc_login_rejects_nonlocal_return_to(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    return_to: str,
) -> None:
    manifest_factory()
    application = create_app(settings_for(tmp_path, auth=_oidc_configuration(monkeypatch)))
    application.state.oidc._discovery = {
        "authorization_endpoint": "https://issuer.example.invalid/authorize",
        "token_endpoint": "https://issuer.example.invalid/token",
        "jwks_uri": "https://issuer.example.invalid/jwks",
    }
    with TestClient(application, base_url="https://workspace.example.test") as client:
        response = client.get(
            f"/api/auth/login?return_to={return_to}",
            follow_redirects=False,
        )
        assert response.status_code == 302
        transient_token = client.cookies.get("kitsune_oidc_state")
        assert transient_token is not None
        transient = application.state.oidc.decode(transient_token)
        assert transient["return_to"] == "/"


@pytest.mark.parametrize(
    ("token_algorithm", "jwk_algorithm", "jwk_use", "advertised"),
    [
        ("none", "RS256", "sig", ["RS256"]),
        ("HS256", "RS256", "sig", ["RS256", "HS256"]),
        ("RS256", "RS512", "sig", ["RS256"]),
        ("RS256", "RS256", "enc", ["RS256"]),
        ("RS256", "RS256", "sig", ["ES256"]),
    ],
)
def test_oidc_rejects_unsupported_or_mismatched_signing_metadata(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
    token_algorithm: str,
    jwk_algorithm: str,
    jwk_use: str,
    advertised: list[str],
) -> None:
    manifest_factory()
    settings = settings_for(tmp_path, auth=_oidc_configuration(monkeypatch))
    application = create_app(settings)
    discovery = {
        "authorization_endpoint": "https://issuer.example.invalid/authorize",
        "token_endpoint": "https://issuer.example.invalid/token",
        "jwks_uri": "https://issuer.example.invalid/jwks",
        "id_token_signing_alg_values_supported": advertised,
    }
    application.state.oidc._discovery = discovery
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "provider-key", "alg": jwk_algorithm, "use": jwk_use})
    application.state.oidc._jwks = {"keys": [public_jwk]}

    with TestClient(application, base_url="https://workspace.example.test") as client:
        login = client.get("/api/auth/login", follow_redirects=False)
        assert login.status_code == 302
        transient_token = client.cookies.get("kitsune_oidc_state")
        assert transient_token is not None
        transient = application.state.oidc.decode(transient_token)
        now = int(time.time())
        claims = {
            "iss": settings.auth.issuer,
            "aud": settings.auth.client_id,
            "sub": "oidc-user",
            "nonce": transient["nonce"],
            "iat": now,
            "exp": now + 300,
        }
        if token_algorithm == "none":  # noqa: S105 - intentionally insecure test token
            id_token = jwt.encode(
                claims,
                key="",
                algorithm="none",
                headers={"kid": "provider-key"},
            )
        elif token_algorithm == "HS256":  # noqa: S105 - intentionally insecure test token
            id_token = jwt.encode(
                claims,
                key="provider-controlled-secret-32-bytes",
                algorithm="HS256",
                headers={"kid": "provider-key"},
            )
        else:
            id_token = jwt.encode(
                claims,
                key=private_key,
                algorithm="RS256",
                headers={"kid": "provider-key"},
            )

        class ProviderClient:
            def __init__(self, **_: Any) -> None:
                return None

            async def __aenter__(self) -> ProviderClient:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                return None

            async def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={"id_token": id_token},
                    request=httpx.Request("POST", url),
                )

        monkeypatch.setattr(security_module.httpx, "AsyncClient", ProviderClient)
        callback = client.get(
            f"/api/auth/callback?code=authorization-code&state={transient['state']}",
            follow_redirects=False,
        )
        assert callback.status_code == 401
        assert client.cookies.get("kitsune_session") is None


def test_fixed_window_rate_limiter_expires_and_evicts_lru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr(security_module.time, "monotonic", lambda: now)
    limiter = FixedWindowRateLimiter(maximum_keys=2)
    limiter.check("a", 10, window_seconds=60)
    limiter.check("b", 10, window_seconds=60)
    limiter.check("a", 10, window_seconds=60)
    limiter.check("c", 10, window_seconds=60)
    assert list(limiter._windows) == ["a", "c"]

    now = 61.0
    limiter.check("d", 10, window_seconds=60)
    assert list(limiter._windows) == ["d"]


def test_rbac_matrix_for_viewer_operator_and_admin(
    tmp_path: Path,
    manifest_factory: ManifestFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_factory()
    settings = settings_for(tmp_path, auth=_oidc_configuration(monkeypatch))
    with TestClient(create_app(settings), base_url="https://workspace.example.test") as client:
        assert client.get("/api/agents").status_code == 401
        csrf = "csrf-matrix-value"

        def assume_role(role: str) -> None:
            token = client.app.state.oidc.encode(
                {
                    "sub": f"{role}-user",
                    "name": f"{role.title()} User",
                    "roles": [role],
                    "csrf": csrf,
                },
                600,
            )
            client.cookies.set("kitsune_session", token)

        headers = {"X-CSRF-Token": csrf}
        assume_role("viewer")
        assert client.get("/api/agents").status_code == 200
        assert client.get("/api/runs").status_code == 200
        assert client.get("/api/audit").status_code == 200
        assert (
            client.post(
                "/api/agents/demo-agent/runs",
                json={"handler": "default", "input": {}},
                headers=headers,
            ).status_code
            == 403
        )
        assert client.post("/api/agents/demo-agent/start", headers=headers).status_code == 403
        assert client.post("/api/admin/reload", headers=headers).status_code == 403
        assert client.get("/api/admin/tokens").status_code == 403

        assume_role("operator")
        created = client.post(
            "/api/agents/demo-agent/runs",
            json={"handler": "default", "input": {}},
            headers=headers,
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        assert client.post(f"/api/runs/{run_id}/cancel", headers=headers).status_code == 200
        assert client.post("/api/agents/demo-agent/stop", headers=headers).status_code == 200
        assert client.post("/api/admin/reload", headers=headers).status_code == 403
        assert client.get("/api/admin/tokens").status_code == 403

        assume_role("admin")
        assert client.post("/api/admin/reload", headers=headers).status_code == 200
        issued = client.post(
            "/api/admin/tokens",
            json={"agent_id": "demo-agent", "description": "RBAC test"},
            headers=headers,
        )
        assert issued.status_code == 200
        assert issued.json()["token"].startswith("kt_agent_")
        assert client.get("/api/admin/tokens").status_code == 200
        assert client.post("/api/agents/demo-agent/start", headers=headers).status_code == 200
