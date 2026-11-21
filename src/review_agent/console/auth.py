"""FastAPI Users wiring and console routes for accounts and sessions."""

import argparse
import asyncio
import getpass
import uuid
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from fastapi_users import FastAPIUsers
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .accounts import (
    SESSION_SECONDS,
    Account,
    AccountPage,
    AccountUpdate,
    NewAccount,
    PasswordChange,
    RegistrationSettings,
    SessionStrategy,
    SessionToken,
    User,
    UserCreate,
    UserManager,
    account_audit,
    database_engine,
    lock_accounts,
    registration_settings,
)
from . import account_admin, registration
from .registration import (
    RegistrationAvailability,
    RegistrationComplete,
    RegistrationSignup,
    RegistrationUpdate,
)
from .email import (
    EmailSettings,
    EmailUpdate,
    EmailTest,
    email_settings,
    save_email_settings,
    send_test_email,
)
from ..settings import PostgresDatabaseUrl, ReviewAgentSettings
from ..postgres.team_access import AccessRequest
from ..postgres.audit import AuditAction


class ConsoleAuth:
    def __init__(
        self,
        database_url: PostgresDatabaseUrl,
        public_url: str,
        *,
        email_secret: Fernet | None = None,
    ) -> None:
        parsed = urlsplit(public_url)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or parsed.scheme not in ("http", "https")
            or (
                parsed.scheme == "http"
                and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            )
        ):
            raise ValueError(
                "REVIEW_AGENT_ADMIN_PUBLIC_URL must be an HTTPS origin (HTTP is allowed only on localhost)"
            )
        self.origin = public_url.rstrip("/")
        self.hostname = parsed.hostname
        self.engine = database_engine(database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async def session_dependency() -> AsyncIterator[AsyncSession]:
            async with self.sessions() as session:
                yield session

        async def manager_dependency(
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> UserManager:
            return UserManager(session)

        async def strategy_dependency(
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> DatabaseStrategy[User, uuid.UUID, SessionToken]:
            return SessionStrategy(session)

        secure = parsed.scheme == "https"
        transport = CookieTransport(
            cookie_name="__Host-review_agent_session"
            if secure
            else "review_agent_session",
            cookie_max_age=SESSION_SECONDS,
            cookie_secure=secure,
            cookie_samesite="strict",
        )
        self.transport = transport
        backend = AuthenticationBackend[User, uuid.UUID](
            name="cookie", transport=transport, get_strategy=strategy_dependency
        )
        users = FastAPIUsers[User, uuid.UUID](manager_dependency, [backend])
        self.current_user = users.current_user(active=True)
        self.current_admin = users.current_user(active=True, superuser=True)

        async def current_owner(
            user: Annotated[User, Depends(self.current_admin)],
        ) -> User:
            if not user.is_platform_owner:
                raise HTTPException(403, "Platform owner access required")
            return user

        self.current_owner = current_owner

        def current_scope(
            user: Annotated[User, Depends(self.current_user)],
            selected_team_id: Annotated[
                int | None, Query(alias="team_id", ge=1, le=9223372036854775807)
            ] = None,
        ) -> AccessRequest:
            return AccessRequest(user.id, selected_team_id)

        self.current_scope = current_scope
        self.auth_router = users.get_auth_router(backend)
        self.router = APIRouter()

        async def registration_availability(
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> RegistrationAvailability:
            policy = await registration_settings(session)
            delivery = await email_settings(session, email_secret)
            configured = delivery.enabled and (
                not delivery.password_set or delivery.credential_storage_available
            )
            return RegistrationAvailability(
                enabled=bool(
                    configured
                    and policy.enabled
                    and (policy.allowed_domains or policy.allowed_emails)
                ),
                email_configured=configured,
            )

        async def register(
            change: RegistrationSignup,
            background: BackgroundTasks,
        ) -> None:
            await registration.request_registration(
                self.sessions, change, background,
                email_secret=email_secret, origin=self.origin,
            )

        async def complete_registration(
            change: RegistrationComplete,
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> Response:
            return await registration.complete_registration(
                session, change, manager=manager, backend=backend
            )

        async def get_registration(
            _actor: Annotated[User, Depends(self.current_owner)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> RegistrationSettings:
            return await registration_settings(session)

        async def save_registration(
            change: RegistrationUpdate,
            actor: Annotated[User, Depends(self.current_owner)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> RegistrationSettings:
            return await registration.save_registration_policy(session, actor, change)

        async def get_email(
            _actor: Annotated[User, Depends(self.current_owner)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> EmailSettings:
            return await email_settings(session, email_secret)

        async def save_email(
            change: EmailUpdate,
            actor: Annotated[User, Depends(self.current_owner)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> EmailSettings:
            return await save_email_settings(
                session, actor, change, email_secret=email_secret
            )

        async def test_email(
            change: EmailTest,
            actor: Annotated[User, Depends(self.current_owner)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> None:
            await send_test_email(session, actor, change, email_secret=email_secret)

        async def me(user: Annotated[User, Depends(self.current_user)]) -> Account:
            return Account.from_user(user)

        async def list_users(
            _actor: Annotated[User, Depends(self.current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            offset: Annotated[int, Query(ge=0, le=10000)] = 0,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
        ) -> AccountPage:
            return await account_admin.list_accounts(
                session, offset=offset, limit=limit
            )

        async def create_user(
            account: NewAccount,
            actor: Annotated[User, Depends(self.current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> Account:
            return await account_admin.create_account(
                session, actor, account, manager=manager
            )

        async def update_user(
            user_id: uuid.UUID,
            change: AccountUpdate,
            actor: Annotated[User, Depends(self.current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> Account:
            return await account_admin.update_account(
                session, actor, user_id, change, manager=manager
            )

        async def change_password(
            change: PasswordChange,
            user: Annotated[User, Depends(self.current_user)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> None:
            await account_admin.change_password(session, user, change, manager=manager)

        self.router.add_api_route("/api/email", get_email, methods=["GET"])
        self.router.add_api_route("/api/email", save_email, methods=["PUT"])
        self.router.add_api_route(
            "/api/email/test", test_email, methods=["POST"], status_code=204
        )
        self.router.add_api_route("/api/me", me, methods=["GET"])
        self.router.add_api_route(
            "/api/registration", get_registration, methods=["GET"]
        )
        self.router.add_api_route(
            "/api/registration", save_registration, methods=["PUT"]
        )
        self.router.add_api_route(
            "/api/auth/registration", registration_availability, methods=["GET"]
        )
        self.router.add_api_route(
            "/api/auth/register", register, methods=["POST"], status_code=202
        )
        self.router.add_api_route(
            "/api/auth/register/complete",
            complete_registration,
            methods=["POST"],
            status_code=204,
        )
        self.router.add_api_route("/api/users", list_users, methods=["GET"])
        self.router.add_api_route(
            "/api/users", create_user, methods=["POST"], status_code=201
        )
        self.router.add_api_route(
            "/api/users/{user_id}", update_user, methods=["PATCH"]
        )
        self.router.add_api_route(
            "/api/account/password", change_password, methods=["POST"], status_code=204
        )


async def bootstrap_admin(
    database_url: PostgresDatabaseUrl, email: str, password: str
) -> None:
    """Create the first account explicitly; never reset credentials on restart."""
    engine = database_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await lock_accounts(session)
            if await session.scalar(select(func.count()).select_from(User)):
                raise ValueError(
                    "Accounts already exist. Sign in as an administrator to manage users."
                )
            manager = UserManager(session)
            user = await manager.create(
                UserCreate.model_validate(
                    {
                        "email": email,
                        "password": password,
                        "is_superuser": True,
                        "is_platform_owner": True,
                    }
                )
            )
            await account_audit(
                session,
                actor=None,
                user=user,
                action=AuditAction.ACCOUNT_CREATED,
                reason="Initial platform owner created",
                details={"email": user.email, "role": "owner"},
            )
            await session.commit()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the first admin-panel account after database preparation."
    )
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Password (15–128 characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords did not match")
    settings = ReviewAgentSettings.from_environment()
    asyncio.run(bootstrap_admin(settings.postgres_database_url, args.email, password))
    print("Administrator created. Sign in with your email and password.")


if __name__ == "__main__":
    main()
