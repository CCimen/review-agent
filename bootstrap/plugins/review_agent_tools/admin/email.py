"""Optional SMTP delivery for console registration verification, and its settings."""

import asyncio
import json
import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..postgres.audit import AuditAction
from ..settings import SettingsError
from .accounts import User, lock_accounts


class SMTPConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9.:-]+$")
    port: int = Field(default=587, ge=1, le=65535)
    sender: EmailStr
    tls: Literal["starttls", "implicit"] = "starttls"
    username: str = Field(default="", max_length=320)


class SMTPSettings(SMTPConfiguration):
    password: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def paired_credentials(self) -> "SMTPSettings":
        if bool(self.username) != bool(self.password.get_secret_value()):
            raise ValueError("SMTP username and password must be set together")
        return self

    def send_registration(self, email: str, link: str) -> None:
        message = EmailMessage()
        message["Subject"] = "Verify your email for Review Agent"
        message["From"] = str(self.sender)
        message["To"] = email
        message.set_content(
            "Open this link to verify your email and choose your Review Agent password:\n\n"
            f"{link}\n\n"
            "The link expires in 30 minutes and works once. "
            "If you did not request an account, ignore this email.\n"
        )
        self.send(message)

    def send_test(self, email: str) -> None:
        message = EmailMessage()
        message["Subject"] = "Review Agent email delivery test"
        message["From"] = str(self.sender)
        message["To"] = email
        message.set_content("Your Review Agent email settings can deliver mail.\n")
        self.send(message)

    def send(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        connection = (
            smtplib.SMTP_SSL(self.host, self.port, timeout=10, context=context)
            if self.tls == "implicit"
            else smtplib.SMTP(self.host, self.port, timeout=10)
        )
        with connection as smtp:
            if self.tls == "starttls":
                smtp.starttls(context=context)
            if self.username:
                smtp.login(self.username, self.password.get_secret_value())
            smtp.send_message(message)


class EmailSettings(BaseModel):
    revision: int
    enabled: bool
    configuration: SMTPConfiguration | None
    password_set: bool
    credential_storage_available: bool


class EmailUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    enabled: bool = Field(strict=True)
    configuration: SMTPConfiguration
    password: SecretStr | None = Field(default=None, max_length=4096)


class EmailTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


def email_cipher(key: str) -> Fernet | None:
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, UnicodeError):
        raise SettingsError(
            "REVIEW_AGENT_EMAIL_SECRET_KEY must be a Fernet encryption key"
        ) from None


async def email_settings(session: AsyncSession, cipher: Fernet | None) -> EmailSettings:
    row = (
        (
            await session.execute(
                text(
                    "SELECT revision, enabled, configuration, encrypted_password IS NOT NULL AS password_set "
                    "FROM review_agent.admin_email_settings WHERE singleton"
                )
            )
        )
        .mappings()
        .one()
    )
    return EmailSettings.model_validate(
        {**row, "credential_storage_available": cipher is not None}
    )


async def smtp_settings(
    session: AsyncSession, cipher: Fernet | None, *, for_test: bool = False
) -> SMTPSettings | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT enabled, configuration, encrypted_password FROM review_agent.admin_email_settings WHERE singleton"
                )
            )
        )
        .mappings()
        .one()
    )
    if row["configuration"] is None or (not row["enabled"] and not for_test):
        return None
    password = ""
    if row["encrypted_password"] is not None:
        if cipher is None:
            raise HTTPException(
                503,
                "Email credentials are unavailable. Check the deployment encryption key.",
            )
        try:
            password = cipher.decrypt(row["encrypted_password"].encode()).decode()
        except (InvalidToken, UnicodeError):
            raise HTTPException(
                503,
                "Email credentials could not be opened. Restore the encryption key or save the password again.",
            ) from None
    return SMTPSettings.model_validate({**row["configuration"], "password": password})


async def save_email_settings(
    session: AsyncSession,
    actor: User,
    change: EmailUpdate,
    *,
    email_secret: Fernet | None,
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


async def send_test_email(
    session: AsyncSession,
    actor: User,
    change: EmailTest,
    *,
    email_secret: Fernet | None,
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
