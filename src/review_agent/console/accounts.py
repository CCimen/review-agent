"""Console accounts, revocable sessions and the account-change lock."""

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Annotated

from fastapi import HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import BaseUserManager, UUIDIDMixin, exceptions, schemas
from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users_db_sqlalchemy import (
    SQLAlchemyBaseUserTableUUID,
    SQLAlchemyUserDatabase,
)
from fastapi_users_db_sqlalchemy.access_token import (
    SQLAlchemyAccessTokenDatabase,
    SQLAlchemyBaseAccessTokenTable,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
)
from sqlalchemy import DateTime, ForeignKey, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..domain.access import Role
from ..postgres.audit import AuditAction
from ..settings import PostgresDatabaseUrl


SESSION_SECONDS = 8 * 60 * 60
REGISTRATION_SECONDS = 30 * 60
REGISTRATION_RESEND_SECONDS = 60
REGISTRATION_REQUESTS_PER_MINUTE = 30
REGISTRATION_MAX_PENDING = 1000
EMAIL_ADDRESS: TypeAdapter[EmailStr] = TypeAdapter(EmailStr)


class RegistrationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)
    allowed_domains: list[Annotated[str, Field(min_length=1, max_length=254)]] = Field(
        default_factory=list, max_length=100
    )
    allowed_emails: list[EmailStr] = Field(default_factory=list, max_length=100)

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        return sorted(
            {
                str(
                    EMAIL_ADDRESS.validate_python(
                        "registration@" + value.strip().removeprefix("@")
                    )
                )
                .rsplit("@", 1)[1]
                .lower()
                for value in values
            }
        )

    @field_validator("allowed_emails")
    @classmethod
    def normalize_emails(cls, values: list[EmailStr]) -> list[EmailStr]:
        return [
            EMAIL_ADDRESS.validate_python(value)
            for value in sorted({str(value).lower() for value in values})
        ]

    def allows(self, email: str) -> bool:
        normalized = str(EMAIL_ADDRESS.validate_python(email)).lower()
        return self.enabled and (
            normalized in self.allowed_emails
            or normalized.rsplit("@", 1)[1] in self.allowed_domains
        )


class RegistrationSettings(RegistrationPolicy):
    revision: int = Field(ge=0)


async def registration_settings(session: AsyncSession) -> RegistrationSettings:
    row = (
        (
            await session.execute(
                text(
                    "SELECT revision, enabled, allowed_domains, allowed_emails "
                    "FROM review_agent.admin_registration_policy WHERE singleton"
                )
            )
        )
        .mappings()
        .one()
    )
    return RegistrationSettings.model_validate(row)


class Base(DeclarativeBase):
    pass


class RegistrationRequest(Base):
    __tablename__ = "admin_registration_requests"
    __table_args__ = {"schema": "review_agent"}
    email: Mapped[str] = mapped_column(primary_key=True)
    token_digest: Mapped[str] = mapped_column(unique=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
            role=Role.from_flags(
                platform_owner=user.is_platform_owner,
                superuser=user.is_superuser,
                global_viewer=user.is_global_viewer,
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


async def account_audit(
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
        await account_audit(
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
            await account_audit(
                self.session,
                actor=user,
                user=user,
                action=AuditAction.SIGNED_OUT,
                reason="Signed out by account holder",
                details={},
            )
            await super().destroy_token(token, user)


def database_engine(database_url: PostgresDatabaseUrl) -> AsyncEngine:
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


async def lock_accounts(session: AsyncSession) -> None:
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


async def lock_registration_requests(session: AsyncSession) -> None:
    # Counts, resends and consumption share one lock; sign-in needs only accounts.
    await session.execute(
        text(
            "LOCK TABLE review_agent.admin_registration_requests IN SHARE ROW EXCLUSIVE MODE"
        )
    )


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
    await lock_accounts(session)
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
    await account_audit(
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
    await lock_accounts(session)
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
    await account_audit(
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


async def sign_in_oidc_account(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    email_verified: bool,
    link_user_id: uuid.UUID | None,
    link_access_revision: int | None,
    link_session_token: str | None,
    register_account: bool = False,
) -> str:
    """Resolve or link the account and issue its audited, revocable session."""
    await lock_accounts(session)
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
    if (
        user is None
        and register_account
        and link_user_id is None
        and email
        and email_verified
    ):
        policy = await registration_settings(session)
        if not policy.allows(email):
            raise PermissionError("Registration is not allowed for this email")
        try:
            user = await UserManager(session).create(
                UserCreate(
                    email=EMAIL_ADDRESS.validate_python(email.lower()),
                    password=secrets.token_urlsafe(48),
                    is_verified=True,
                )
            )
        except exceptions.UserAlreadyExists as exc:
            raise PermissionError(
                "An existing account must be explicitly linked"
            ) from exc
        user.oidc_issuer, user.oidc_subject = issuer, subject
        await account_audit(
            session,
            actor=user,
            user=user,
            action=AuditAction.ACCOUNT_CREATED,
            reason="Registered with a verified organization identity",
            details={
                "email": user.email,
                "role": Role.MEMBER.value,
                "method": "self_registration",
                "issuer": issuer,
            },
        )
    if user is None or not user.is_active or user.scim_deleted:
        raise PermissionError(
            "An active provisioned or explicitly linked account is required"
        )
    if user.oidc_issuer is None:
        if not email_verified or email is None or user.email.lower() != email.lower():
            raise PermissionError("First linking requires a matching verified email")
        user.oidc_issuer, user.oidc_subject = issuer, subject
        await account_audit(
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
    token = await SessionStrategy(session, method="oidc").write_token(user)
    await UserManager(session).on_after_login(user)
    return token
