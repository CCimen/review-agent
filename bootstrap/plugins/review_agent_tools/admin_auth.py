"""Individual operator accounts and revocable sessions using FastAPI Users."""

import argparse
import asyncio
import getpass
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
from sqlalchemy import ForeignKey, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .settings import PostgresDatabaseUrl, ReviewAgentSettings


SESSION_SECONDS = 8 * 60 * 60


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "admin_users"
    __table_args__ = {"schema": "review_agent"}


class SessionToken(SQLAlchemyBaseAccessTokenTable[uuid.UUID], Base):
    __tablename__ = "admin_sessions"
    __table_args__ = {"schema": "review_agent"}
    if TYPE_CHECKING:
        user_id: uuid.UUID
    else:
        user_id: Mapped[uuid.UUID] = mapped_column(
            ForeignKey("review_agent.admin_users.id")
        )


class Role(StrEnum):
    VIEWER = "viewer"
    ADMIN = "admin"


class Account(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    active: bool

    @classmethod
    def from_user(cls, user: User) -> "Account":
        return cls(
            id=user.id,
            email=user.email,
            role=Role.ADMIN if user.is_superuser else Role.VIEWER,
            active=user.is_active,
        )


class NewAccount(BaseModel):
    email: EmailStr
    password: Annotated[SecretStr, Field(min_length=15, max_length=128)]
    role: Role = Role.VIEWER


class AccountUpdate(BaseModel):
    role: Role | None = None
    active: bool | None = None
    password: Annotated[SecretStr | None, Field(min_length=15, max_length=128)] = None


class PasswordChange(BaseModel):
    current_password: Annotated[SecretStr, Field(max_length=128)]
    password: Annotated[SecretStr, Field(min_length=15, max_length=128)]


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(SQLAlchemyUserDatabase[User, uuid.UUID](session, User))
        self.session = session

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
            return DatabaseStrategy(
                SQLAlchemyAccessTokenDatabase(session, SessionToken),
                lifetime_seconds=SESSION_SECONDS,
            )

        secure = parsed.scheme == "https"
        transport = CookieTransport(
            cookie_name="__Host-review_agent_session"
            if secure
            else "review_agent_session",
            cookie_max_age=SESSION_SECONDS,
            cookie_secure=secure,
            cookie_samesite="strict",
        )
        backend = AuthenticationBackend[User, uuid.UUID](
            name="cookie", transport=transport, get_strategy=strategy_dependency
        )
        users = FastAPIUsers[User, uuid.UUID](manager_dependency, [backend])
        self.current_user = users.current_user(active=True)
        current_admin = users.current_user(active=True, superuser=True)
        self.auth_router = users.get_auth_router(backend)
        self.router = APIRouter()

        async def me(user: Annotated[User, Depends(self.current_user)]) -> Account:
            return Account.from_user(user)

        async def list_users(
            _actor: Annotated[User, Depends(current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        ) -> list[Account]:
            result = await session.scalars(
                select(User).order_by(func.lower(User.email)).offset(offset).limit(51)
            )
            return [Account.from_user(user) for user in result]

        async def create_user(
            account: NewAccount,
            actor: Annotated[User, Depends(current_admin)],
            session: Annotated[AsyncSession, Depends(session_dependency)],
            manager: Annotated[UserManager, Depends(manager_dependency)],
        ) -> Account:
            await _lock_accounts(session)
            await session.refresh(actor)
            if not actor.is_active or not actor.is_superuser:
                raise HTTPException(403, "Administrator access required")
            try:
                user = await manager.create(
                    schemas.BaseUserCreate(
                        email=account.email,
                        password=account.password.get_secret_value(),
                        is_superuser=account.role is Role.ADMIN,
                    )
                )
            except exceptions.UserAlreadyExists as exc:
                raise HTTPException(
                    409, "An account with this email already exists"
                ) from exc
            return Account.from_user(user)

        async def update_user(
            user_id: uuid.UUID,
            change: AccountUpdate,
            actor: Annotated[User, Depends(current_admin)],
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
            if (
                user.is_active
                and user.is_superuser
                and (change.active is False or change.role is Role.VIEWER)
            ):
                count = await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.__table__.c.is_active, User.__table__.c.is_superuser)
                )
                if count is None or count <= 1:
                    raise HTTPException(409, "Keep at least one active administrator")
            values = schemas.BaseUserUpdate()
            if change.role is not None:
                values.is_superuser = change.role is Role.ADMIN
            if change.active is not None:
                values.is_active = change.active
            if change.password is not None:
                values.password = change.password.get_secret_value()
            await session.execute(
                delete(SessionToken).where(SessionToken.__table__.c.user_id == user.id)
            )
            # The library commits the account update and session revocation in
            # this same transaction. Disabled accounts cannot resume old sessions.
            return Account.from_user(await manager.update(values, user))

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
            await session.execute(
                delete(SessionToken).where(SessionToken.__table__.c.user_id == user.id)
            )
            await manager.update(
                schemas.BaseUserUpdate(password=change.password.get_secret_value()),
                user,
                safe=True,
            )

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
            await manager.create(
                schemas.BaseUserCreate.model_validate(
                    {"email": email, "password": password, "is_superuser": True}
                )
            )
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
