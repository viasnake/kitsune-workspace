"""OIDC sessions, RBAC, CSRF, Agent tokens, and request rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlparse, urlsplit

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, Response, Security, status
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK
from sqlalchemy.orm import Session

from .config import AuthSection, resolve_secret_reference
from .models import AgentCredential, AgentDefinition
from .util import ensure_aware, nested, utcnow

Role = Literal["viewer", "operator", "admin"]
ROLE_LEVEL: dict[str, int] = {"viewer": 0, "operator": 1, "admin": 2}
OIDC_SIGNING_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)

# These optional security dependencies only describe the credentials accepted by the
# routes.  Authentication remains enforced by the dependencies below so local mode and
# the existing error messages retain their current behavior.
agent_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AgentBearer",
    bearerFormat="Kitsune Agent token",
    description="Bearer token issued for one Agent.",
)
session_cookie = APIKeyCookie(
    name="kitsune_session",
    auto_error=False,
    scheme_name="SessionCookie",
    description=(
        "Signed Workspace session cookie used when auth.mode is oidc; "
        "auth.mode=none uses the loopback development boundary."
    ),
)


def _local_return_to(value: str) -> str:
    """Return a safe local absolute-path reference or the Workspace root."""

    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    return value


@dataclass(frozen=True)
class Principal:
    """Authenticated human identity used by API authorization checks."""

    subject: str
    name: str
    roles: tuple[Role, ...]
    csrf_token: str

    def permits(self, role: Role) -> bool:
        """Return whether any assigned role meets the required role."""

        required = ROLE_LEVEL[role]
        return any(ROLE_LEVEL[item] >= required for item in self.roles)


class OIDCManager:
    """OIDC Authorization Code + PKCE flow and signed secure sessions."""

    transient_cookie = "kitsune_oidc_state"

    def __init__(self, config: AuthSection) -> None:
        self.config = config
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None

    @property
    def secret(self) -> str:
        """Return the configured Workspace session-signing secret."""

        secret = self.config.session_secret
        if secret is None:
            raise RuntimeError("OIDC session secret is not configured")
        return secret.get_secret_value()

    @property
    def redirect_uri(self) -> str:
        """Return the configured canonical callback URI."""

        redirect_uri = self.config.redirect_uri
        if redirect_uri is None:  # pragma: no cover - rejected by AuthSection validation
            raise RuntimeError("OIDC redirect URI is not configured")
        return redirect_uri

    def encode(self, claims: dict[str, Any], ttl_seconds: int) -> str:
        """Sign short-lived state or session claims."""

        now = utcnow()
        payload = {**claims, "iat": now, "exp": now + timedelta(seconds=ttl_seconds)}
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def decode(self, token: str) -> dict[str, Any]:
        """Verify locally signed state or session claims."""

        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session"
            ) from exc

    async def discovery(self) -> dict[str, Any]:
        """Fetch and cache the provider discovery document."""

        if self._discovery is None:
            issuer = str(self.config.issuer).rstrip("/")
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{issuer}/.well-known/openid-configuration")
                response.raise_for_status()
                document = response.json()
                if not isinstance(document, dict):
                    raise RuntimeError("OIDC discovery document must be a JSON object")
                discovered_issuer = document.get("issuer")
                if not isinstance(discovered_issuer, str) or not hmac.compare_digest(
                    discovered_issuer.rstrip("/"), issuer
                ):
                    raise RuntimeError("OIDC discovery issuer does not match configured issuer")
                for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    endpoint = document.get(key)
                    parsed = urlparse(endpoint) if isinstance(endpoint, str) else None
                    if (
                        parsed is None
                        or parsed.scheme != "https"
                        or not parsed.netloc
                        or parsed.username
                        or parsed.password
                        or parsed.fragment
                    ):
                        raise RuntimeError(f"OIDC discovery {key} must be an absolute HTTPS URL")
                self._discovery = document
        discovery = self._discovery
        if discovery is None:
            raise RuntimeError("OIDC discovery did not return a document")
        return discovery

    async def login(self, request: Request, response: Response) -> str:
        """Create state, nonce, and PKCE values and return the provider authorization URL."""

        discovery = await self.discovery()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(
            b"="
        )
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        return_to = _local_return_to(request.query_params.get("return_to", "/"))
        transient = self.encode(
            {"state": state, "nonce": nonce, "verifier": verifier, "return_to": return_to},
            600,
        )
        response.set_cookie(
            self.transient_cookie,
            transient,
            max_age=600,
            httponly=True,
            secure=self.config.secure_cookie,
            samesite="lax",
            path="/api/auth/callback",
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
            }
        )
        return f"{discovery['authorization_endpoint']}?{query}"

    async def callback(self, request: Request, response: Response) -> str:
        """Exchange the authorization code, verify the ID token, and set a session cookie."""

        transient_token = request.cookies.get(self.transient_cookie)
        if not transient_token:
            raise HTTPException(status_code=400, detail="missing OIDC state cookie")
        transient = self.decode(transient_token)
        if not hmac.compare_digest(str(request.query_params.get("state", "")), transient["state"]):
            raise HTTPException(status_code=400, detail="OIDC state mismatch")
        code = request.query_params.get("code")
        if not code:
            raise HTTPException(status_code=400, detail="missing OIDC authorization code")
        discovery = await self.discovery()
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                discovery["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "client_id": self.config.client_id,
                    "client_secret": (
                        self.config.client_secret.get_secret_value()
                        if self.config.client_secret
                        else None
                    ),
                    "code_verifier": transient["verifier"],
                },
            )
            token_response.raise_for_status()
            tokens = token_response.json()
            if self._jwks is None:
                jwks_response = await client.get(discovery["jwks_uri"])
                jwks_response.raise_for_status()
                self._jwks = jwks_response.json()
        id_token = tokens.get("id_token")
        if not id_token:
            raise HTTPException(status_code=400, detail="provider omitted ID token")
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="invalid ID token header") from exc
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in OIDC_SIGNING_ALGORITHMS:
            raise HTTPException(status_code=401, detail="unsupported ID token signing algorithm")
        supported = discovery.get("id_token_signing_alg_values_supported")
        if supported is not None and (
            not isinstance(supported, list) or algorithm not in supported
        ):
            raise HTTPException(
                status_code=401,
                detail="ID token signing algorithm is not advertised by the provider",
            )
        jwks = self._jwks or {}
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise HTTPException(status_code=401, detail="provider JWKS is invalid")
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise HTTPException(status_code=401, detail="ID token signing key ID is missing")
        key_data = next(
            (item for item in keys if isinstance(item, dict) and item.get("kid") == key_id),
            None,
        )
        if key_data is None:
            self._jwks = None
            raise HTTPException(status_code=401, detail="ID token signing key not found")
        if key_data.get("alg") is not None and key_data.get("alg") != algorithm:
            raise HTTPException(status_code=401, detail="ID token signing key algorithm mismatch")
        if key_data.get("use") is not None and key_data.get("use") != "sig":
            raise HTTPException(status_code=401, detail="ID token key is not a signing key")
        key_operations = key_data.get("key_ops")
        if key_operations is not None and (
            not isinstance(key_operations, list) or "verify" not in key_operations
        ):
            raise HTTPException(status_code=401, detail="ID token key cannot verify signatures")
        try:
            claims = jwt.decode(
                id_token,
                PyJWK.from_dict(key_data, algorithm=algorithm).key,
                algorithms=[algorithm],
                audience=self.config.client_id,
                issuer=self.config.issuer,
            )
        except (jwt.PyJWTError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="invalid ID token") from exc
        authorized_party = claims.get("azp")
        audience = claims.get("aud")
        client_id = self.config.client_id
        if client_id is None:  # pragma: no cover - rejected by AuthSection validation
            raise RuntimeError("OIDC client ID is not configured")
        if authorized_party is not None and (
            not isinstance(authorized_party, str)
            or not hmac.compare_digest(authorized_party, client_id)
        ):
            raise HTTPException(status_code=401, detail="ID token authorized party mismatch")
        if isinstance(audience, list) and len(audience) > 1 and authorized_party is None:
            raise HTTPException(status_code=401, detail="ID token authorized party is missing")
        if not hmac.compare_digest(str(claims.get("nonce", "")), transient["nonce"]):
            raise HTTPException(status_code=401, detail="ID token nonce mismatch")
        raw_roles = claims.get(self.config.role_claim, [])
        if isinstance(raw_roles, str):
            raw_roles = [raw_roles]
        roles = [item for item in raw_roles if item in ROLE_LEVEL] or [self.config.default_role]
        csrf_token = secrets.token_urlsafe(32)
        session = self.encode(
            {
                "sub": claims["sub"],
                "name": claims.get("name") or claims.get("email") or claims["sub"],
                "roles": roles,
                "csrf": csrf_token,
            },
            self.config.session_ttl_seconds,
        )
        response.delete_cookie(self.transient_cookie, path="/api/auth/callback")
        response.set_cookie(
            self.config.session_cookie_name,
            session,
            max_age=self.config.session_ttl_seconds,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return str(transient["return_to"])

    def principal(self, request: Request) -> Principal:
        """Read an authenticated principal from the signed session cookie."""

        if self.config.mode == "none":
            return Principal("local-development", "Local administrator", ("admin",), "")
        token = request.cookies.get(self.config.session_cookie_name)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
            )
        claims = self.decode(token)
        roles = tuple(item for item in claims.get("roles", []) if item in ROLE_LEVEL)
        if not roles:
            roles = (self.config.default_role,)
        return Principal(claims["sub"], claims["name"], roles, claims["csrf"])

    def logout(self, response: Response) -> None:
        """Remove the browser session cookie."""

        response.delete_cookie(self.config.session_cookie_name, path="/")


def principal_dependency(
    request: Request,
    _: str | None = Security(session_cookie),
) -> Principal:
    """FastAPI dependency returning the configured human principal."""

    manager: OIDCManager = request.app.state.oidc
    return manager.principal(request)


def agent_authorization(
    credentials: HTTPAuthorizationCredentials | None = Security(agent_bearer),
) -> str:
    """Return the existing Authorization value after documenting Bearer auth."""

    if credentials is None:
        raise HTTPException(status_code=401, detail="Agent bearer token required")
    return f"{credentials.scheme} {credentials.credentials}"


def require_role(role: Role) -> Callable[..., Principal]:
    """Build an RBAC and CSRF dependency for one minimum role."""

    def dependency(
        request: Request, principal: Principal = Depends(principal_dependency)
    ) -> Principal:
        if not principal.permits(role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.app.state.settings.auth.mode == "oidc"
        ):
            supplied = request.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, principal.csrf_token):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch"
                )
        return principal

    return dependency


def _encode_issued_token_hash(secret: str) -> str:
    digest = hashlib.sha256(secret.encode()).digest()
    digest_text = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"sha256${digest_text}"


def _encode_bootstrap_audit_hash(secret: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    salt_text = base64.urlsafe_b64encode(salt).decode()
    digest_text = base64.urlsafe_b64encode(digest).decode()
    return f"scrypt${salt_text}${digest_text}"


def _matches_issued_token_hash(secret: str, encoded: str) -> bool:
    try:
        algorithm, expected = encoded.split("$", 1)
        if algorithm != "sha256":
            return False
        actual = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).decode()
        actual = actual.rstrip("=")
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual.encode(), expected.encode())


def issue_agent_token(
    session: Session,
    agent_id: str,
    description: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[AgentCredential, str]:
    """Create a high-entropy Agent bearer token and persist only its SHA-256 digest."""

    credential_id = str(uuid.uuid4())
    secret = secrets.token_urlsafe(32)
    credential = AgentCredential(
        credential_id=credential_id,
        agent_id=agent_id,
        kind="issued",
        token_hash=_encode_issued_token_hash(secret),
        description=description,
        issued_at=utcnow(),
        expires_at=expires_at,
    )
    session.add(credential)
    return credential, f"kt_agent_{credential_id}.{secret}"


def verify_agent_token(session: Session, authorization: str | None) -> AgentCredential:
    """Verify one scoped bearer token and update its last-used timestamp."""

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Agent bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    prefix, separator, secret = token.partition(".")
    if not separator or not prefix.startswith("kt_agent_") or not secret:
        raise HTTPException(status_code=401, detail="invalid Agent bearer token")
    credential = session.get(AgentCredential, prefix.removeprefix("kt_agent_"))
    now = utcnow()
    if credential is None or credential.revoked_at is not None:
        raise HTTPException(status_code=401, detail="invalid Agent bearer token")
    if credential.kind != "issued":
        raise HTTPException(status_code=401, detail="invalid Agent bearer token")
    if credential.expires_at is not None and ensure_aware(credential.expires_at) <= now:
        raise HTTPException(status_code=401, detail="expired Agent bearer token")
    if not _matches_issued_token_hash(secret, credential.token_hash):
        raise HTTPException(status_code=401, detail="invalid Agent bearer token")
    credential.last_used_at = now
    return credential


def verify_agent_access(
    session: Session,
    authorization: str | None,
    claimed_agent_id: str,
) -> AgentCredential:
    """Verify issued credentials or bootstrap a Manifest-referenced deployment token.

    The bootstrap token remains in environment or a secret file. Every request resolves that
    current source of truth; the deterministic audit credential is created only on first use.
    """

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Agent bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    if token.startswith("kt_agent_"):
        credential = verify_agent_token(session, authorization)
        if credential.agent_id != claimed_agent_id:
            raise HTTPException(status_code=403, detail="Agent token scope mismatch")
        return credential
    definition = session.get(AgentDefinition, claimed_agent_id)
    if definition is None or not definition.active:
        raise HTTPException(status_code=401, detail="unknown Agent Definition")
    reference = nested(definition.snapshot, "spec", "security", "agent_token_ref")
    if not isinstance(reference, str):
        raise HTTPException(status_code=401, detail="Agent has no bootstrap token reference")
    credential_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"kitsune:{claimed_agent_id}:manifest-token")
    )
    credential = session.get(AgentCredential, credential_id)
    if credential is not None and credential.revoked_at is not None:
        raise HTTPException(status_code=401, detail="revoked Agent bootstrap credential")
    try:
        expected = resolve_secret_reference(reference)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=401, detail="Agent bootstrap secret is unavailable"
        ) from exc
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid Agent bearer token")
    if credential is None:
        credential = AgentCredential(
            credential_id=credential_id,
            agent_id=claimed_agent_id,
            kind="manifest_bootstrap",
            token_hash=_encode_bootstrap_audit_hash(token),
            description="Manifest bootstrap token",
            issued_at=utcnow(),
        )
        session.add(credential)
    credential.last_used_at = utcnow()
    return credential


class FixedWindowRateLimiter:
    """Bounded in-process rate limiter for one active Workspace instance."""

    def __init__(self, maximum_keys: int = 10_000) -> None:
        if maximum_keys < 1:
            raise ValueError("maximum_keys must be positive")
        self.maximum_keys = maximum_keys
        self._windows: OrderedDict[str, tuple[int, float, int]] = OrderedDict()
        self._next_cleanup = 0.0

    def _cleanup(self, current: float) -> None:
        if current < self._next_cleanup:
            return
        expired = [
            key
            for key, (_, started, duration) in self._windows.items()
            if current - started >= duration
        ]
        for key in expired:
            self._windows.pop(key, None)
        self._next_cleanup = current + 1.0

    def check(self, key: str, limit: int, window_seconds: int = 60) -> None:
        """Consume one request or raise HTTP 429."""

        current = time.monotonic()
        self._cleanup(current)
        existing = self._windows.pop(key, None)
        count, started, _ = existing or (0, current, window_seconds)
        if current - started >= window_seconds:
            count, started = 0, current
        if count >= limit:
            self._windows[key] = (count, started, window_seconds)
            retry_after = max(1, int(window_seconds - (current - started)))
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        if existing is None and len(self._windows) >= self.maximum_keys:
            self._windows.popitem(last=False)
        self._windows[key] = (count + 1, started, window_seconds)
