"""Self-registration by verified email and the policy that allows it."""

import asyncio
import json
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated

from cryptography.fernet import Fernet
from fastapi import HTTPException, Response
from fastapi_users import exceptions
from fastapi_users.authentication import AuthenticationBackend
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.access import Role
from ..postgres.audit import AuditAction
from .accounts import (
    EMAIL_ADDRESS,
    REGISTRATION_MAX_PENDING,
    REGISTRATION_REQUESTS_PER_MINUTE,
    REGISTRATION_RESEND_SECONDS,
    REGISTRATION_SECONDS,
    RegistrationPolicy,
    RegistrationRequest,
    RegistrationSettings,
    SessionStrategy,
    User,
    UserCreate,
    UserManager,
    account_audit,
    lock_accounts,
    lock_registration_requests,
    registration_settings,
)
from .email import smtp_settings


class RegistrationUpdate(RegistrationPolicy):
    expected_revision: int = Field(ge=0)
    reason: str = Field(
        default="Registration settings updated", min_length=1, max_length=500
    )


class RegistrationAvailability(BaseModel):
    enabled: bool
    email_configured: bool


class RegistrationSignup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class RegistrationComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: Annotated[SecretStr, Field(min_length=43, max_length=43)]
    password: Annotated[SecretStr, Field(min_length=15, max_length=128)]


async def request_registration(
    session: AsyncSession,
    change: RegistrationSignup,
    *,
    email_secret: Fernet | None,
    origin: str,
) -> None:
    smtp = await smtp_settings(session, email_secret)
    if smtp is None:
        raise HTTPException(
            503,
            "Email registration is unavailable. Contact your administrator.",
        )
    policy = await registration_settings(session)
    if not policy.enabled:
        raise HTTPException(
            403, "Registration is closed. Contact your administrator."
        )
    email = str(change.email).lower()
    if not policy.allows(email):
        return
    await lock_registration_requests(session)
    if await session.scalar(
        select(User).where(func.lower(User.email) == email)
    ):
        return
    now = datetime.now(timezone.utc)
    await session.execute(
        delete(RegistrationRequest).where(RegistrationRequest.expires_at < now)
    )
    pending = await session.get(RegistrationRequest, email)
    if pending and pending.requested_at > now - timedelta(
        seconds=REGISTRATION_RESEND_SECONDS
    ):
        return
    recent = await session.scalar(
        select(func.count())
        .select_from(RegistrationRequest)
        .where(RegistrationRequest.requested_at > now - timedelta(minutes=1))
    )
    total = await session.scalar(
        select(func.count()).select_from(RegistrationRequest)
    )
    if (recent or 0) >= REGISTRATION_REQUESTS_PER_MINUTE or (
        pending is None and (total or 0) >= REGISTRATION_MAX_PENDING
    ):
        raise HTTPException(
            429,
            "Registration is busy. Try again in a minute.",
            headers={"Retry-After": "60"},
        )
    token = secrets.token_urlsafe(32)
    if pending is None:
        pending = RegistrationRequest(email=email)
        session.add(pending)
    pending.token_digest = sha256(token.encode()).hexdigest()
    pending.requested_at = now
    pending.expires_at = now + timedelta(seconds=REGISTRATION_SECONDS)
    await session.commit()
    # Commit the cooldown before delivery and release the request lock while
    # SMTP runs. Interrupted sends can be retried after one minute.
    try:
        await asyncio.to_thread(
            smtp.send_registration,
            email,
            f"{origin}/#register_token={token}",
        )
    except (OSError, smtplib.SMTPException):
        raise HTTPException(
            503,
            "Could not send the verification email. Wait one minute and try again.",
        ) from None


async def complete_registration(
    session: AsyncSession,
    change: RegistrationComplete,
    *,
    manager: UserManager,
    backend: AuthenticationBackend[User, uuid.UUID],
) -> Response:
    await lock_registration_requests(session)
    pending = await session.scalar(
        select(RegistrationRequest).where(
            RegistrationRequest.token_digest
            == sha256(change.token.get_secret_value().encode()).hexdigest()
        )
    )
    if pending is None or pending.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            400,
            "This verification link expired or was already used. Request a new link.",
        )
    await lock_accounts(session)
    policy = await registration_settings(session)
    if not policy.allows(pending.email):
        raise HTTPException(
            403,
            "Registration is no longer allowed for this email. Contact your administrator.",
        )
    try:
        user = await manager.create(
            UserCreate(
                email=EMAIL_ADDRESS.validate_python(pending.email),
                password=change.password.get_secret_value(),
                is_verified=True,
            )
        )
    except exceptions.UserAlreadyExists as exc:
        raise HTTPException(
            400,
            "This registration cannot be completed. Sign in or contact your administrator.",
        ) from exc
    await account_audit(
        session,
        actor=user,
        user=user,
        action=AuditAction.ACCOUNT_CREATED,
        reason="Registered after email verification",
        details={
            "email": user.email,
            "role": Role.MEMBER.value,
            "method": "email_registration",
        },
    )
    await session.delete(pending)
    await session.flush()
    response = await backend.login(SessionStrategy(session), user)
    await manager.on_after_login(user)
    return response


async def save_registration_policy(
    session: AsyncSession,
    actor: User,
    change: RegistrationUpdate,
) -> RegistrationSettings:
    await lock_accounts(session)
    await session.refresh(actor)
    if not actor.is_active or not actor.is_platform_owner:
        raise HTTPException(403, "Platform owner access required")
    if not change.reason.strip():
        raise HTTPException(422, "A reason is required")
    previous = await registration_settings(session)
    if previous.revision != change.expected_revision:
        raise HTTPException(
            409, "Registration settings changed. Reload before saving."
        )
    await session.execute(
        text(
            "UPDATE review_agent.admin_registration_policy "
            "SET revision = revision + 1, enabled = :enabled, "
            "allowed_domains = CAST(:domains AS JSONB), allowed_emails = CAST(:emails AS JSONB) "
            "WHERE singleton"
        ),
        {
            "enabled": change.enabled,
            "domains": json.dumps(change.allowed_domains),
            "emails": json.dumps(change.allowed_emails),
        },
    )
    await session.execute(
        text(
            "INSERT INTO review_agent.admin_audit_events "
            "(actor_id, actor_email, actor_role, action, subject, reason, details, owner_only) "
            "VALUES (:actor_id, :email, 'owner', :action, 'self-registration', :reason, CAST(:details AS JSONB), true)"
        ),
        {
            "actor_id": actor.id,
            "email": actor.email,
            "action": AuditAction.REGISTRATION_UPDATED.value,
            "reason": change.reason.strip(),
            "details": json.dumps(
                {
                    "previous_revision": previous.revision,
                    "enabled": change.enabled,
                    "allowed_domains": json.dumps(change.allowed_domains),
                    "allowed_emails": json.dumps(change.allowed_emails),
                }
            ),
        },
    )
    result = await registration_settings(session)
    await session.commit()
    return result
