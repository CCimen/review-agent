"""Individual operator accounts and revocable sessions using FastAPI Users."""

import argparse
import asyncio
import getpass
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import (
    BaseUserManager,
    FastAPIUsers,
    UUIDIDMixin,
    exceptions,
    schemas,
)
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users_db_sqlalchemy import (
    SQLAlchemyBaseUserTableUUID,
    SQLAlchemyUserDatabase,
)
from fastapi_users_db_sqlalchemy.access_token import (
    SQLAlchemyAccessTokenDatabase,
    SQLAlchemyBaseAccessTokenTable,
)
from pydantic import BaseModel, EmailStr, Field, SecretStr
from sqlalchemy import DateTime, ForeignKey, delete, func, select, text, true
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column

from .domain.access import Role
from .settings import PostgresDatabaseUrl, ReviewAgentSettings
from .postgres.team_access import AccessRequest
from .postgres.audit import AuditAction


SESSION_SECONDS = 8 * 60 * 60


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "admin_users"
    __table_args__ = {"schema": "review_agent"}
    is_global_viewer: Mapped[bool] = mapped_column(default=False)
    is_platform_owner: Mapped[bool] = mapped_column(default=False)
    access_revision: Mapped[int] = mapped_column(default=0)
    oidc_issuer: Mapped[str | None] = mapped_column(default=None)
    oidc_subject: Mapped[str | None] = mapped_column(default=None)
    scim_issuer: Mapped[str | None] = mapped_column(default=None)
    scim_external_id: Mapped[str | None] = mapped_column(default=None)
    scim_deleted: Mapped[bool] = mapped_column(default=False)
    scim_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scim_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SessionToken(SQLAlchemyBaseAccessTokenTable[uuid.UUID], Base):
    __tablename__ = "admin_sessions"
    __table_args__ = {"schema": "review_agent"}
    if TYPE_CHECKING:
        user_id: uuid.UUID
    else:
        user_id: Mapped[uuid.UUID] = mapped_column(
            ForeignKey("review_agent.admin_users.id")
        )


class Account(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    active: bool
    access_revision: int = 0

    @classmethod
    def from_user(cls, user: User) -> "Account":
        return cls(
            id=user.id,
            email=user.email,
            role=(
                Role.OWNER
                if user.is_platform_owner
                else Role.ADMIN
                if user.is_superuser
                else Role.VIEWER
                if user.is_global_viewer
                else Role.MEMBER
            ),
            active=user.is_active,
            access_revision=user.access_revision,
        )


class AccountPage(BaseModel):
    items: list[Account]
    total: int
    admin_count: int
    disabled_count: int
    has_more: bool


class NewAccount(BaseModel):
    email: EmailStr
    password: Annotated[SecretStr, Field(min_length=15, max_length=128)]
    role: Role = Role.MEMBER
    reason: str = Field(default="Account created", min_length=1, max_length=500)


class UserCreate(schemas.BaseUserCreate):
    is_platform_owner: bool = False
    is_global_viewer: bool = False


class UserUpdate(schemas.BaseUserUpdate):
    is_platform_owner: bool | None = None
    is_global_viewer: bool | None = None


class AccountUpdate(BaseModel):
    reason: str = Field(default="Account access updated", min_length=1, max_length=500)
    role: Role | None = None
    active: bool | None = None
    password: Annotated[SecretStr | None, Field(min_length=15, max_length=128)] = None


class PasswordChange(BaseModel):
    current_password: Annotated[SecretStr, Field(max_length=128)]
    password: Annotated[SecretStr, Field(min_length=15, max_length=128)]


class UserDatabase(SQLAlchemyUserDatabase[User, uuid.UUID]):
    """Let the account operation commit credentials, revocation and audit together."""

    async def create(self, create_dict: dict[str, object]) -> User:
        user = User(**create_dict)
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user

    async def update(self, user: User, update_dict: dict[str, object]) -> User:
        for key, value in update_dict.items():
            setattr(user, key, value)
        await self.session.flush()
        await self.session.refresh(user)
        return user


async def _account_audit(
    session: AsyncSession,
    *,
    actor: User | None,
    user: User,
    action: AuditAction,
    reason: str,
    details: dict[str, str | int | bool | None],
    was_privileged: bool = False,
    actor_role: Role | None = None,
    actor_source: str = "bootstrap",
) -> None:
    await session.execute(
        text("""INSERT INTO review_agent.admin_audit_events
            (actor_id, actor_email, actor_role, action, subject, reason, details, owner_only)
            VALUES (:actor_id, :actor_email, :actor_role, :action, :subject, :reason, CAST(:details AS JSONB), :owner_only)"""),
        {
            "actor_id": actor.id if actor else None,
            "actor_email": actor.email if actor else None,
            "actor_role": (actor_role or Account.from_user(actor).role).value
            if actor
            else actor_source,
            "action": action.value,
            "subject": str(user.id),
            "reason": reason,
            "details": json.dumps(details),
            "owner_only": was_privileged or user.is_superuser,
        },
    )


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(UserDatabase(session, User))
        self.session = session

    async def authenticate(self, credentials: OAuth2PasswordRequestForm) -> User | None:
        # Authentication and any password rehash complete before revocation can
        # take the account-change lock and invalidate the issued session.
        await self.session.execute(
            text("LOCK TABLE review_agent.admin_users IN SHARE MODE")
        )
        return await super().authenticate(credentials)

    async def on_after_login(
        self,
        user: User,
        request: Request | None = None,
        response: Response | None = None,
    ) -> None:
        expired = (
            select(SessionToken.__table__.c.token)
            .where(
                SessionToken.__table__.c.created_at
                < datetime.now(timezone.utc) - timedelta(seconds=SESSION_SECONDS)
            )
            .limit(1000)
        )
        await self.session.execute(
            delete(SessionToken).where(SessionToken.__table__.c.token.in_(expired))
        )
        await self.session.commit()

    async def validate_password(
        self, password: str, user: schemas.BaseUserCreate | User
    ) -> None:
        if not 15 <= len(password) <= 128:
            raise exceptions.InvalidPasswordException(
                reason="Use a password of 15–128 characters."
            )


class SessionStrategy(DatabaseStrategy[User, uuid.UUID, SessionToken]):
    def __init__(self, session: AsyncSession, *, method: str = "password") -> None:
        super().__init__(
            SQLAlchemyAccessTokenDatabase(session, SessionToken),
            lifetime_seconds=SESSION_SECONDS,
        )
        self.session = session
        self.method = method

    async def write_token(self, user: User) -> str:
        await self.session.execute(
            text("LOCK TABLE review_agent.admin_users IN SHARE MODE")
        )
        await self.session.refresh(user)
        if not user.is_active:
            raise HTTPException(401, "Sign in to continue")
        await _account_audit(
            self.session,
            actor=user,
            user=user,
            action=AuditAction.SIGNED_IN,
            reason="Sign-in succeeded",
            details={"method": self.method},
        )
        return await super().write_token(user)

    async def destroy_token(self, token: str, user: User) -> None:
        if await self.database.get_by_token(token) is not None:
            await _account_audit(
                self.session,
                actor=user,
                user=user,
                action=AuditAction.SIGNED_OUT,
                reason="Signed out by account holder",
                details={},
            )
            await super().destroy_token(token, user)


def _engine(database_url: PostgresDatabaseUrl) -> AsyncEngine:
    return create_async_engine(
        make_url(str(database_url)).set(drivername="postgresql+psycopg"),
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "options": "-c timezone=UTC -c statement_timeout=15000 -c lock_timeout=2000"
        },
    )


async def _lock_accounts(session: AsyncSession) -> None:
    # User changes are infrequent. Serialize them to preserve an active admin
    # even when two administrators change each other's access concurrently.
    await session.execute(
        text("LOCK TABLE review_agent.admin_users IN SHARE ROW EXCLUSIVE MODE")
    )


class ProvisioningConflict(ValueError):
    """A directory operation conflicts with an existing console account."""


class ProvisionedAccountUpdate(BaseModel):
    email: EmailStr | None = None
    external_id: str | None = None
    active: bool | None = None


async def revoke_account_sessions(session: AsyncSession, user: User) -> None:
    await session.execute(
        delete(SessionToken).where(SessionToken.__table__.c.user_id == user.id)
    )
    user.access_revision += 1


async def provision_account(
    session: AsyncSession,
    *,
    issuer: str,
    email: EmailStr,
    external_id: str | None,
    active: bool,
) -> User:
    await _lock_accounts(session)
    if (
        external_id is not None
        and await session.scalar(
            select(User).where(
                User.scim_issuer == issuer, User.scim_external_id == external_id
            )
        )
        is not None
    ):
        raise ProvisioningConflict("An account with this externalId already exists")
    try:
        user = await UserManager(session).create(
            UserCreate(
                email=email,
                password=secrets.token_urlsafe(48),
                is_active=active,
            )
        )
    except exceptions.UserAlreadyExists as exc:
        raise ProvisioningConflict(
            "An account with this userName already exists"
        ) from exc
    user.scim_issuer = issuer
    user.scim_external_id = external_id
    user.scim_created_at = user.scim_modified_at = datetime.now(timezone.utc)
    await _account_audit(
        session,
        actor=None,
        actor_source="scim",
        user=user,
        action=AuditAction.SCIM_PROVISIONED,
        reason="Account provisioned by the organization directory",
        details={"issuer": issuer, "email": user.email, "active": active},
    )
    await session.commit()
    return user


async def update_provisioned_account(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    issuer: str,
    change: ProvisionedAccountUpdate,
    deleted: bool = False,
) -> User:
    await _lock_accounts(session)
    user = await session.get(User, user_id)
    if user is None or user.scim_issuer != issuer or user.scim_deleted:
        raise exceptions.UserNotExists()
    if user.is_superuser or user.is_global_viewer:
        raise PermissionError("Only a platform owner can change privileged accounts")
    if (
        change.external_id is not None
        and await session.scalar(
            select(User).where(
                User.scim_issuer == issuer,
                User.scim_external_id == change.external_id,
                User.__table__.c.id != user_id,
            )
        )
        is not None
    ):
        raise ProvisioningConflict("An account with this externalId already exists")
    previous_active = user.is_active
    values = schemas.BaseUserUpdate()
    if change.email is not None:
        values.email = change.email
    if change.active is not None or deleted:
        values.is_active = False if deleted else change.active
    try:
        await UserManager(session).update(values, user)
    except exceptions.UserAlreadyExists as exc:
        raise ProvisioningConflict(
            "An account with this userName already exists"
        ) from exc
    if "external_id" in change.model_fields_set:
        user.scim_external_id = change.external_id
    user.scim_deleted = deleted
    user.scim_modified_at = datetime.now(timezone.utc)
    await revoke_account_sessions(session, user)
    await _account_audit(
        session,
        actor=None,
        actor_source="scim",
        user=user,
        action=AuditAction.SCIM_DEACTIVATED
        if not user.is_active
        else AuditAction.SCIM_UPDATED,
        reason="Account updated by the organization directory",
        details={
            "issuer": issuer,
            "previous_active": previous_active,
            "active": user.is_active,
            "deleted": deleted,
        },
    )
    await session.commit()
    return user


async def authenticate_oidc_account(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    email_verified: bool,
    link_user_id: uuid.UUID | None,
    link_access_revision: int | None,
    link_session_token: str | None,
) -> User:
    await _lock_accounts(session)
    user = await session.scalar(
        select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
    )
    if link_user_id is not None:
        initiating_session = (
            await session.get(SessionToken, link_session_token)
            if link_session_token
            else None
        )
        target = await session.get(User, link_user_id)
        if (
            target is None
            or not target.is_active
            or target.access_revision != link_access_revision
            or initiating_session is None
            or initiating_session.user_id != link_user_id
            or initiating_session.created_at
            < datetime.now(timezone.utc) - timedelta(seconds=SESSION_SECONDS)
            or (user is not None and user.id != link_user_id)
        ):
            raise PermissionError("The account link request is no longer valid")
        user = target
    elif user is None and email and email_verified:
        user = await session.scalar(
            select(User).where(
                func.lower(User.email) == email.lower(),
                User.scim_issuer == issuer,
                ~User.scim_deleted,
                ~User.__table__.c.is_superuser,
                ~User.is_global_viewer,
            )
        )
    if user is None or not user.is_active or user.scim_deleted:
        raise PermissionError(
            "An active provisioned or explicitly linked account is required"
        )
    if user.oidc_issuer is None:
        if not email_verified or email is None or user.email.lower() != email.lower():
            raise PermissionError("First linking requires a matching verified email")
        user.oidc_issuer, user.oidc_subject = issuer, subject
        await _account_audit(
            session,
            actor=user,
            user=user,
            action=AuditAction.IDENTITY_LINKED,
            reason="Organization identity linked to the account",
            details={"issuer": issuer, "method": "account" if link_user_id else "scim"},
        )
    elif (user.oidc_issuer, user.oidc_subject) != (issuer, subject):
        raise PermissionError("This account is linked to another organization identity")
    await session.flush()
    return user


class AdminAuth:
    def __init__(self, database_url: PostgresDatabaseUrl, public_url: str) -> None:
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
        self.engine = _engine(database_url)
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
            await _lock_accounts(session)
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
            await _account_audit(
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
            await _lock_accounts(session)
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
            await _account_audit(
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
            await _lock_accounts(session)
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
            await _account_audit(
                session,
                actor=user,
                user=user,
                action=AuditAction.PASSWORD_CHANGED,
                reason="Password changed by account holder",
                details={},
            )
            await session.commit()

        self.router.add_api_route("/api/me", me, methods=["GET"])
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
    engine = _engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _lock_accounts(session)
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
            await _account_audit(
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
