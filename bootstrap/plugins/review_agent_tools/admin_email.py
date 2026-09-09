"""Optional SMTP delivery for console registration verification."""

import smtplib
import ssl
from email.message import EmailMessage
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .settings import SettingsError


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
