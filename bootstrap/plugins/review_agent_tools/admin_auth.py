"""FastAPI Users wiring and console routes for accounts and sessions."""

import argparse
import asyncio
import getpass
import json
import smtplib
import uuid
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi_users import FastAPIUsers, exceptions, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from sqlalchemy import func, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from .domain.access import Role
from .admin.accounts import (
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
    UserUpdate,
    account_audit,
    database_engine,
    lock_accounts,
    registration_settings,
    revoke_account_sessions,
)
from .admin import registration
from .admin.registration import (
    RegistrationAvailability,
    RegistrationComplete,
    RegistrationSignup,
    RegistrationUpdate,
)
from .admin.email import (
    EmailSettings,
    EmailUpdate,
    EmailTest,
    email_settings,
    smtp_settings,
)
from .settings import PostgresDatabaseUrl, ReviewAgentSettings
from .postgres.team_access import AccessRequest
from .postgres.audit import AuditAction


class AdminAuth:
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
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> None:
            await registration.request_registration(
                session, change, email_secret=email_secret, origin=self.origin
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
            await lock_accounts(session)
            await session.refresh(actor)
            if not actor.is_active or not actor.is_platform_owner:
                raise HTTPException(403, "Platform owner access required")
            previous = await email_settings(session, email_secret)
            if previous.revision != change.expected_revision:
                raise HTTPException(
                    409, "Email settings changed. Reload before saving."
                )
            # A changed relay must not receive a password retained for another endpoint.
            if (
                change.password is None
                and previous.password_set
                and previous.configuration
            ):
                old = previous.configuration
                new = change.configuration
                if (old.host, old.port, old.tls, old.username) != (
                    new.host,
                    new.port,
                    new.tls,
                    new.username,
                ):
                    raise HTTPException(
                        422,
                        "Enter the password again when changing the SMTP server or login.",
                    )
            retained = await session.scalar(
                text(
                    "SELECT encrypted_password FROM review_agent.admin_email_settings WHERE singleton"
                )
            )
            if change.password is not None:
                password = change.password.get_secret_value()
                if password and email_secret is None:
                    raise HTTPException(
                        503,
                        "Configure the deployment email encryption key before saving a password.",
                    )
                retained = (
                    email_secret.encrypt(password.encode()).decode()
                    if password and email_secret
                    else None
                )
            if bool(change.configuration.username) != bool(retained):
                raise HTTPException(
                    422, "SMTP username and password must be set together."
                )
            await session.execute(
                text(
                    "UPDATE review_agent.admin_email_settings SET revision = revision + 1, "
                    "enabled = :enabled, configuration = CAST(:configuration AS JSONB), encrypted_password = :password WHERE singleton"
                ),
                {
                    "enabled": change.enabled,
                    "configuration": change.configuration.model_dump_json(),
                    "password": retained,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO review_agent.admin_audit_events "
                    "(actor_id, actor_email, actor_role, action, subject, reason, details, owner_only) "
                    "VALUES (:id, :email, 'owner', :action, 'email-delivery', 'Email settings updated', CAST(:details AS JSONB), true)"
                ),
                {
                    "id": actor.id,
                    "email": actor.email,
                    "action": AuditAction.EMAIL_UPDATED.value,
                    "details": json.dumps(
                        {
                            "previous_revision": previous.revision,
                            "enabled": change.enabled,
                            "host": change.configuration.host,
                            "password_changed": change.password is not None,
                        }
                    ),
                },
            )
            result = await email_settings(session, email_secret)
            await session.commit()
            return result

        async def test_email(
            change: EmailTest,
            actor: Annotated[User, Depends(self.current_owner)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
        ) -> None:
            await lock_accounts(session)
            await session.refresh(actor)
            if not actor.is_active or not actor.is_platform_owner:
                raise HTTPException(403, "Platform owner access required")
            settings = await email_settings(session, email_secret)
            if settings.revision != change.expected_revision:
                raise HTTPException(
                    409, "Email settings changed. Reload before testing."
                )
            smtp = await smtp_settings(session, email_secret, for_test=True)
            if smtp is None:
                raise HTTPException(422, "Save SMTP settings before testing.")
            claimed = await session.scalar(
                text(
                    "UPDATE review_agent.admin_email_settings SET last_test_at = now() "
                    "WHERE singleton AND (last_test_at IS NULL OR last_test_at < now() - interval '1 minute') RETURNING singleton"
                )
            )
            if not claimed:
                raise HTTPException(
                    429,
                    "Wait one minute before sending another test.",
                    headers={"Retry-After": "60"},
                )
            await session.commit()
            try:
                await asyncio.to_thread(smtp.send_test, actor.email)
            except (OSError, smtplib.SMTPException):
                raise HTTPException(
                    503,
                    "Could not send the test email. Check the SMTP settings and try again in a minute.",
                ) from None

        async def me(user: Annotated[User, Depends(self.current_user)]) -> Account:
            return Account.from_user(user)

        async def list_users(
            _actor: Annotated[User, Depends(self.current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            offset: Annotated[int, Query(ge=0, le=10000)] = 0,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
        ) -> AccountPage:
            totals = (
                select(
                    func.count().label("total"),
                    func.count().filter(User.__table__.c.is_superuser).label("admins"),
                    func.count().filter(~User.__table__.c.is_active).label("disabled"),
                )
                .select_from(User)
                .cte("totals")
            )
            page = (
                select(User)
                .order_by(func.lower(User.email), User.__table__.c.id)
                .offset(offset)
                .limit(limit + 1)
                .subquery()
            )
            account = aliased(User, page)
            result = await session.execute(
                select(account, totals.c.total, totals.c.admins, totals.c.disabled)
                .select_from(totals.outerjoin(page, true()))
                .order_by(func.lower(account.email), page.c.id)
            )
            rows = result.all()
            items = [Account.from_user(row[0]) for row in rows if row[0] is not None]
            return AccountPage(
                items=items[:limit],
                total=rows[0][1],
                admin_count=rows[0][2],
                disabled_count=rows[0][3],
                has_more=len(items) > limit,
            )

        async def create_user(
            account: NewAccount,
            actor: Annotated[User, Depends(self.current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> Account:
            await lock_accounts(session)
            await session.refresh(actor)
            if not actor.is_active or not actor.is_superuser:
                raise HTTPException(403, "Administrator access required")
            if account.role in (Role.ADMIN, Role.OWNER) and not actor.is_platform_owner:
                raise HTTPException(403, "Only owners can manage privileged accounts")
            try:
                user = await manager.create(
                    UserCreate(
                        email=account.email,
                        password=account.password.get_secret_value(),
                        is_superuser=account.role in (Role.ADMIN, Role.OWNER),
                        is_platform_owner=account.role is Role.OWNER,
                        is_global_viewer=account.role is Role.VIEWER,
                    )
                )
            except exceptions.UserAlreadyExists as exc:
                raise HTTPException(
                    409, "An account with this email already exists"
                ) from exc
            await account_audit(
                session,
                actor=actor,
                user=user,
                action=AuditAction.ACCOUNT_CREATED,
                reason=account.reason,
                details={"email": user.email, "role": account.role.value},
            )
            await session.commit()
            return Account.from_user(user)

        async def update_user(
            user_id: uuid.UUID,
            change: AccountUpdate,
            actor: Annotated[User, Depends(self.current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> Account:
            await lock_accounts(session)
            await session.refresh(actor)
            if not actor.is_active or not actor.is_superuser:
                raise HTTPException(403, "Administrator access required")
            user = await session.get(User, user_id)
            if user is None:
                raise HTTPException(404, "Account not found")
            if not actor.is_platform_owner and (
                user.is_superuser or change.role in (Role.ADMIN, Role.OWNER)
            ):
                raise HTTPException(403, "Only owners can manage privileged accounts")
            if (
                user.is_active
                and user.is_platform_owner
                and (
                    change.active is False
                    or (change.role is not None and change.role is not Role.OWNER)
                )
            ):
                count = await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.__table__.c.is_active, User.__table__.c.is_platform_owner
                    )
                )
                if count is None or count <= 1:
                    raise HTTPException(409, "Keep at least one active owner")
            previous = Account.from_user(user)
            actor_snapshot = Account.from_user(actor)
            values = UserUpdate()
            if change.role is not None:
                values.is_superuser = change.role in (Role.ADMIN, Role.OWNER)
                values.is_platform_owner = change.role is Role.OWNER
                values.is_global_viewer = change.role is Role.VIEWER
            if change.active is not None:
                values.is_active = change.active
            if change.password is not None:
                values.password = change.password.get_secret_value()
            await revoke_account_sessions(session, user)
            updated = await manager.update(values, user)
            await account_audit(
                session,
                actor=actor,
                user=updated,
                action=AuditAction.ACCOUNT_UPDATED,
                reason=change.reason,
                was_privileged=previous.role in (Role.ADMIN, Role.OWNER),
                actor_role=actor_snapshot.role,
                details={
                    "previous_role": previous.role.value,
                    "role": Account.from_user(updated).role.value,
                    "previous_active": previous.active,
                    "active": updated.is_active,
                    "password_reset": change.password is not None,
                },
            )
            await session.commit()
            return Account.from_user(updated)

        async def change_password(
            change: PasswordChange,
            user: Annotated[User, Depends(self.current_user)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> None:
            await lock_accounts(session)
            await session.refresh(user)
            if not user.is_active:
                raise HTTPException(401, "Sign in to continue")
            verified, _ = manager.password_helper.verify_and_update(
                change.current_password.get_secret_value(), user.hashed_password
            )
            if not verified:
                raise HTTPException(400, "Current password is incorrect")
            await revoke_account_sessions(session, user)
            await manager.update(
                schemas.BaseUserUpdate(password=change.password.get_secret_value()),
                user,
                safe=True,
            )
            await account_audit(
                session,
                actor=user,
                user=user,
                action=AuditAction.PASSWORD_CHANGED,
                reason="Password changed by account holder",
                details={},
            )
            await session.commit()

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
