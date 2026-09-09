"""Generic authorization-code OIDC with durable, browser-bound sign-in requests."""

import asyncio
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, Protocol, cast

import httpx2
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, KeySetSerialization
from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    JsonValue,
    field_validator,
)
from sqlalchemy import DateTime, ForeignKey, delete, select
from sqlalchemy.orm import Mapped, mapped_column
from starlette.responses import RedirectResponse

from .admin_auth import (
    AdminAuth,
    Base,
    User,
    sign_in_oidc_account,
)
from .admin_identity_config import IdentitySettings, OIDCSettings, identity_https_url


REQUEST_SECONDS = 5 * 60
SIGNING_ALGORITHMS = {
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
}


class _OAuthClient(Protocol):
    """The Authlib client operations used at the provider boundary."""

    async def request(
        self, method: str, url: str, *, withhold_token: bool
    ) -> httpx2.Response: ...

    async def fetch_token(
        self, url: str, *, code: str, code_verifier: str
    ) -> object: ...

    def create_authorization_url(
        self, url: str, *, state: str, nonce: str, code_verifier: str
    ) -> tuple[str, str]: ...


class OIDCRequest(Base):
    __tablename__ = "admin_oidc_requests"
    __table_args__ = {"schema": "review_agent"}
    state_digest: Mapped[str] = mapped_column(primary_key=True)
    browser_digest: Mapped[str]
    issuer: Mapped[str]
    client_id: Mapped[str]
    nonce: Mapped[str]
    code_verifier: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    link_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_agent.admin_users.id")
    )
    link_access_revision: Mapped[int | None]
    link_session_token: Mapped[str | None] = mapped_column(
        ForeignKey("review_agent.admin_sessions.token")
    )


class Discovery(BaseModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    id_token_signing_alg_values_supported: list[str] = Field(
        default=["RS256"], max_length=30
    )

    @field_validator("issuer")
    @classmethod
    def https_issuer(cls, value: str) -> str:
        return identity_https_url(value)

    @field_validator("authorization_endpoint", "token_endpoint", "jwks_uri")
    @classmethod
    def https_endpoint(cls, value: str) -> str:
        return identity_https_url(value, allow_query=True)


class TokenResponse(BaseModel):
    id_token: str = Field(min_length=1, max_length=16384)
    access_token: str = Field(min_length=1, max_length=16384)


class JWKS(BaseModel):
    keys: list[dict[str, JsonValue]] = Field(min_length=1, max_length=50)


class IdentityClaims(BaseModel):
    iss: str
    sub: str = Field(min_length=1, max_length=255)
    email: EmailStr | None = None
    email_verified: bool = Field(default=False, strict=True)


class OIDCStart(BaseModel):
    authorization_url: str


class IdentityProvider(BaseModel):
    name: str | None


class AccountIdentity(BaseModel):
    provider_name: str | None
    linked: bool


class OIDCHTTPTransport(httpx2.AsyncHTTPTransport):
    """Limit the provider response before OAuth or JSON decoding buffers it."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await super().handle_async_request(request)
        content = bytearray()
        try:
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise ValueError(
                    "Identity provider must return an uncompressed response"
                )
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > 256 * 1024:
                    raise ValueError("Identity provider response exceeds its limit")
                content.extend(chunk)
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx2.Response(
                response.status_code,
                headers=headers,
                content=bytes(content),
                request=request,
            )
        finally:
            await response.aclose()


def oauth_client(settings: OIDCSettings, redirect_uri: str) -> AsyncOAuth2Client:
    return AsyncOAuth2Client(
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        scope="openid email",
        redirect_uri=redirect_uri,
        code_challenge_method="S256",
        token_endpoint_auth_method="client_secret_basic",
        timeout=10,
        follow_redirects=False,
        trust_env=False,
        headers={"Accept-Encoding": "identity"},
        transport=OIDCHTTPTransport(),
    )


async def discovery(client: AsyncOAuth2Client, settings: OIDCSettings) -> Discovery:
    response = await cast(_OAuthClient, client).request(
        "GET",
        settings.issuer.rstrip("/") + "/.well-known/openid-configuration",
        withhold_token=True,
    )
    response.raise_for_status()
    metadata = Discovery.model_validate_json(response.content)
    if metadata.issuer != settings.issuer:
        raise ValueError("The discovered issuer does not match configuration")
    return metadata


async def exchange(
    settings: OIDCSettings, request: OIDCRequest, code: str, redirect_uri: str
) -> IdentityClaims:
    async with oauth_client(settings, redirect_uri) as client:
        oauth = cast(_OAuthClient, client)
        metadata = await discovery(client, settings)
        token = TokenResponse.model_validate(
            await oauth.fetch_token(
                metadata.token_endpoint, code=code, code_verifier=request.code_verifier
            )
        )
        response = await oauth.request("GET", metadata.jwks_uri, withhold_token=True)
        response.raise_for_status()
        jwks = JWKS.model_validate_json(response.content)
        allowed = SIGNING_ALGORITHMS.intersection(
            metadata.id_token_signing_alg_values_supported
        )
        if not allowed:
            raise ValueError("No supported ID token signing algorithm")
        decoded = jwt.decode(
            token.id_token,
            KeySet.import_key_set(cast(KeySetSerialization, jwks.model_dump())),
            algorithms=allowed,
        )
        claims = CodeIDToken(
            decoded.claims,
            decoded.header,
            options={
                "iss": {"value": settings.issuer},
                "aud": {"value": settings.client_id},
            },
            params={
                "nonce": request.nonce,
                "client_id": settings.client_id,
                "access_token": token.access_token,
            },
        )
        claims.validate(leeway=30)
        return IdentityClaims.model_validate(decoded.claims)


def create_router(auth: AdminAuth, identity: IdentitySettings) -> APIRouter:
    router = APIRouter(tags=["organization sign-in"])
    cookie_name = (
        "__Host-review_agent_oidc"
        if auth.origin.startswith("https:")
        else "review_agent_oidc"
    )
    redirect_uri = auth.origin + "/api/auth/oidc/callback"

    def configured() -> OIDCSettings:
        if identity.oidc is None:
            raise HTTPException(404, "Organization sign-in is not configured")
        return identity.oidc

    def provider() -> IdentityProvider:
        return IdentityProvider(name=identity.oidc.name if identity.oidc else None)

    def account_identity(
        user: Annotated[User, Depends(auth.current_user)],
    ) -> AccountIdentity:
        return AccountIdentity(
            provider_name=identity.oidc.name if identity.oidc else None,
            linked=bool(identity.oidc and user.oidc_issuer == identity.oidc.issuer),
        )

    async def begin(
        request: Request, response: Response, user: User | None = None
    ) -> OIDCStart:
        settings = configured()
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        transaction = OIDCRequest(
            state_digest=sha256(state.encode()).hexdigest(),
            browser_digest=sha256(browser.encode()).hexdigest(),
            issuer=settings.issuer,
            client_id=settings.client_id,
            nonce=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=REQUEST_SECONDS),
            link_user_id=user.id if user else None,
            link_access_revision=user.access_revision if user else None,
            link_session_token=request.cookies.get(auth.transport.cookie_name)
            if user
            else None,
        )
        try:
            async with (
                asyncio.timeout(20),
                oauth_client(settings, redirect_uri) as client,
            ):
                metadata = await discovery(client, settings)
                url, _state = cast(_OAuthClient, client).create_authorization_url(
                    metadata.authorization_endpoint,
                    state=state,
                    nonce=transaction.nonce,
                    code_verifier=transaction.code_verifier,
                )
        except (httpx2.HTTPError, OAuthError, ValueError, TimeoutError) as exc:
            raise HTTPException(
                502, "Organization sign-in is unavailable. Try again shortly."
            ) from exc
        async with auth.sessions() as session:
            expired = (
                select(OIDCRequest.state_digest)
                .where(OIDCRequest.expires_at < datetime.now(timezone.utc))
                .limit(1000)
            )
            await session.execute(
                delete(OIDCRequest).where(OIDCRequest.state_digest.in_(expired))
            )
            previous_browser = request.cookies.get(cookie_name)
            if previous_browser:
                await session.execute(
                    delete(OIDCRequest).where(
                        OIDCRequest.browser_digest
                        == sha256(previous_browser.encode()).hexdigest()
                    )
                )
            session.add(transaction)
            await session.commit()
        response.set_cookie(
            cookie_name,
            browser,
            max_age=REQUEST_SECONDS,
            secure=auth.origin.startswith("https:"),
            httponly=True,
            samesite="lax",
        )
        return OIDCStart(authorization_url=url)

    async def start(request: Request, response: Response) -> OIDCStart:
        return await begin(request, response)

    async def link(
        request: Request,
        response: Response,
        user: Annotated[User, Depends(auth.current_user)],
    ) -> OIDCStart:
        return await begin(request, response, user)

    async def callback(
        request: Request,
        state: Annotated[str, Query(min_length=43, max_length=43)],
        code: Annotated[str | None, Query(max_length=8192)] = None,
        error: Annotated[str | None, Query(max_length=255)] = None,
    ) -> Response:
        settings = configured()
        browser = request.cookies.get(cookie_name, "")
        async with auth.sessions() as session:
            transaction = await session.scalar(
                delete(OIDCRequest)
                .where(
                    OIDCRequest.state_digest == sha256(state.encode()).hexdigest(),
                    OIDCRequest.browser_digest == sha256(browser.encode()).hexdigest(),
                )
                .returning(OIDCRequest)
            )
            await session.commit()
        destination = "/account" if transaction and transaction.link_user_id else "/"
        result: Response = RedirectResponse(
            f"{destination}?sso_error=expired", status_code=303
        )
        if (
            transaction is not None
            and transaction.expires_at > datetime.now(timezone.utc)
            and transaction.issuer == settings.issuer
            and transaction.client_id == settings.client_id
            and len(request.query_params.getlist("state")) == 1
            and len(request.query_params.getlist("code")) <= 1
        ):
            if error or not code:
                result = RedirectResponse(
                    f"{destination}?sso_error=cancelled", status_code=303
                )
            else:
                try:
                    async with asyncio.timeout(30):
                        claims = await exchange(
                            settings, transaction, code, redirect_uri
                        )
                    async with auth.sessions() as session:
                        token = await sign_in_oidc_account(
                            session,
                            issuer=claims.iss,
                            subject=claims.sub,
                            email=claims.email,
                            email_verified=claims.email_verified,
                            link_user_id=transaction.link_user_id,
                            link_access_revision=transaction.link_access_revision,
                            link_session_token=transaction.link_session_token,
                        )
                        result = await auth.transport.get_login_response(token)
                        result.status_code = 303
                        result.headers["Location"] = (
                            "/account?identity=linked"
                            if transaction.link_user_id
                            else "/"
                        )
                except PermissionError:
                    result = RedirectResponse(
                        f"{destination}?sso_error=account", status_code=303
                    )
                except (
                    httpx2.HTTPError,
                    OAuthError,
                    JoseError,
                    ValueError,
                    TimeoutError,
                ):
                    result = RedirectResponse(
                        f"{destination}?sso_error=provider", status_code=303
                    )
        result.delete_cookie(
            cookie_name,
            secure=auth.origin.startswith("https:"),
            httponly=True,
            samesite="lax",
        )
        return result

    router.add_api_route("/api/auth/oidc/provider", provider, methods=["GET"])
    router.add_api_route("/api/auth/oidc/start", start, methods=["POST"])
    router.add_api_route("/api/auth/oidc/callback", callback, methods=["GET"])
    router.add_api_route("/api/account/identity", account_identity, methods=["GET"])
    router.add_api_route("/api/account/identity/link", link, methods=["POST"])
    return router
