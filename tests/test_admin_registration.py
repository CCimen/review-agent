from __future__ import annotations

import os
import smtplib
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import psycopg
from cryptography.fernet import Fernet

from tests import test_admin_api
from review_agent_tools.admin_email import SMTPSettings


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")


class SMTPSettingsTests(unittest.TestCase):
    def test_transport_validates_tls_and_paired_credentials(self) -> None:
        values = {"host": "mail.example.test", "sender": "registration@example.com"}
        for overrides in ({"tls": "plain"}, {"port": 0}, {"username": "only-username"}):
            with self.assertRaises(ValueError):
                SMTPSettings.model_validate({**values, **overrides})
        for mode, port, transport in (
            ("starttls", 587, "smtplib.SMTP"),
            ("implicit", 465, "smtplib.SMTP_SSL"),
        ):
            settings = SMTPSettings.model_validate(
                {
                    **values,
                    "tls": mode,
                    "port": port,
                    "username": "local-test-smtp-user",
                    "password": "local-test-smtp-password",
                }
            )
            with patch(transport) as smtp:
                settings.send_registration(
                    "staff@sundsvall.se",
                    "https://admin.example.test/#register_token=local-test-token",
                )
            self.assertEqual(smtp.call_args.args, ("mail.example.test", port))
            self.assertEqual(
                smtp.return_value.__enter__.return_value.starttls.call_count,
                int(mode == "starttls"),
            )
            smtp.return_value.__enter__.return_value.login.assert_called_once_with(
                "local-test-smtp-user", "local-test-smtp-password"
            )


@unittest.skipUnless(DSN, "requires an isolated PostgreSQL test database")
class RegistrationAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "REVIEW_AGENT_EMAIL_SECRET_KEY": Fernet.generate_key().decode(),
                    "REVIEW_AGENT_OIDC_ISSUER": "",
                    "REVIEW_AGENT_OIDC_CLIENT_ID": "",
                    "REVIEW_AGENT_OIDC_CLIENT_SECRET": "",
                    "REVIEW_AGENT_SCIM_TOKEN": "",
                },
            )
        )
        self.smtp = self.enterContext(patch("smtplib.SMTP"))
        self.fixture = test_admin_api.AdminAPITests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.client = self.fixture.client

    def enable_registration(self) -> None:
        self.fixture.login()
        mail = self.client.put(
            "/api/email",
            json={
                "expected_revision": 0,
                "enabled": True,
                "configuration": {
                    "host": "mail.example.test",
                    "sender": "registration@example.com",
                },
            },
        )
        self.assertEqual(mail.status_code, 200, mail.text)
        response = self.client.put(
            "/api/registration",
            json={
                "expected_revision": 0,
                "enabled": True,
                "allowed_domains": ["sundsvall.se"],
                "allowed_emails": ["partner@example.com", "admin@example.com"],
                "reason": "Open staff registration",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.client.post("/api/auth/logout")

    def token(self) -> str:
        message = (
            self.smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        )
        link = next(
            line
            for line in message.get_content().splitlines()
            if line.startswith("https://")
        )
        self.assertEqual(urlsplit(link).netloc, "admin.example.test")
        self.assertFalse(urlsplit(link).query)
        return parse_qs(urlsplit(link).fragment)["register_token"][0]

    def test_owner_configures_email_without_restart_and_secrets_are_redacted(
        self,
    ) -> None:
        self.fixture.login()
        payload = {
            "expected_revision": 0,
            "enabled": True,
            "configuration": {
                "host": "smtp.example.test",
                "port": 587,
                "sender": "registration@example.com",
                "username": "mailer",
            },
            "password": "local-smtp-secret-only",
        }
        saved = self.client.put("/api/email", json=payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["password_set"])
        self.assertNotIn("local-smtp-secret-only", saved.text)
        self.assertEqual(self.client.put("/api/email", json=payload).status_code, 409)
        self.assertEqual(
            self.client.post(
                "/api/email/test", json={"expected_revision": 1}
            ).status_code,
            204,
        )
        message = (
            self.smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
        )
        self.assertEqual(message["To"], "admin@example.com")
        with psycopg.connect(DSN) as connection:
            stored = connection.execute(
                "SELECT encrypted_password FROM review_agent.admin_email_settings"
            ).fetchone()[0]
            self.assertNotIn("local-smtp-secret-only", stored)
            audit = connection.execute(
                "SELECT details::text FROM review_agent.admin_audit_events WHERE action = 'email_updated'"
            ).fetchone()[0]
            self.assertNotIn("local-smtp-secret-only", audit)
        payload.update(expected_revision=1, enabled=False)
        payload.pop("password")
        disabled = self.client.put("/api/email", json=payload)
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(
            self.client.get("/api/auth/registration").json()["email_configured"]
        )
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/email").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/email/test", json={"expected_revision": 2}
            ).status_code,
            401,
        )

    def test_email_settings_protect_credentials_and_restrict_access(self) -> None:
        self.fixture.login()
        payload = {
            "expected_revision": 0,
            "enabled": True,
            "configuration": {
                "host": "smtp.example.test",
                "sender": "registration@example.com",
                "username": "mailer",
            },
            "password": "local-smtp-secret-only",
        }
        self.assertEqual(self.client.put("/api/email", json=payload).status_code, 200)
        payload.update(expected_revision=1)
        payload.pop("password")
        payload["configuration"]["host"] = "different.example.test"
        self.assertEqual(self.client.put("/api/email", json=payload).status_code, 422)
        payload["configuration"]["host"] = "smtp.example.test"
        self.assertEqual(self.client.put("/api/email", json=payload).status_code, 200)
        sender = self.smtp.return_value.__enter__.return_value.send_message
        sender.side_effect = smtplib.SMTPException("private relay failure")
        failed = self.client.post("/api/email/test", json={"expected_revision": 2})
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private relay failure", failed.text)
        self.assertEqual(
            self.client.post(
                "/api/email/test", json={"expected_revision": 2}
            ).status_code,
            429,
        )
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE review_agent.admin_email_settings SET encrypted_password = 'unreadable-ciphertext'"
            )
        self.assertEqual(
            self.client.post(
                "/api/auth/register", json={"email": "staff@sundsvall.se"}
            ).status_code,
            503,
        )
        member = self.client.post(
            "/api/users",
            json={"email": "member@example.com", "password": test_admin_api.PASSWORD},
        )
        self.assertEqual(member.status_code, 201)
        self.client.post("/api/auth/logout")
        self.fixture.login("member@example.com")
        self.assertEqual(self.client.get("/api/email").status_code, 403)
        self.assertEqual(self.client.put("/api/email", json=payload).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/email/test", json={"expected_revision": 2}
            ).status_code,
            403,
        )

    def test_registration_does_not_contend_with_sign_in_account_lock(self) -> None:
        self.enable_registration()
        with ThreadPoolExecutor(max_workers=1) as executor:
            for path, body, expected_status in (
                ("/api/auth/register", {"email": "outside@example.com"}, 202),
                ("/api/auth/register", {"email": "staff@sundsvall.se"}, 202),
                (
                    "/api/auth/register/complete",
                    {"token": "x" * 43, "password": test_admin_api.PASSWORD},
                    400,
                ),
            ):
                with psycopg.connect(DSN) as connection:
                    connection.execute("LOCK review_agent.admin_users IN SHARE MODE")
                    pending = executor.submit(self.client.post, path, json=body)
                    try:
                        response = pending.result(timeout=2)
                        self.assertEqual(
                            response.status_code, expected_status, response.text
                        )
                    finally:
                        connection.rollback()

    def test_verified_registration_creates_member_and_signs_in_without_oidc(
        self,
    ) -> None:
        self.enable_registration()
        self.assertEqual(
            self.client.get("/api/auth/registration").json(),
            {"enabled": True, "email_configured": True},
        )
        for email in ("Staff@Sundsvall.SE", "Partner@example.com"):
            response = self.client.post("/api/auth/register", json={"email": email})
            self.assertEqual(response.status_code, 202, response.text)
            token = self.token()
            self.assertEqual(self.client.get("/api/me").status_code, 401)
            self.assertEqual(
                self.client.post(
                    "/api/auth/login",
                    data={
                        "username": email,
                        "password": test_admin_api.PASSWORD,
                    },
                ).status_code,
                400,
            )
            completed = self.client.post(
                "/api/auth/register/complete",
                json={
                    "token": token,
                    "password": test_admin_api.PASSWORD,
                },
            )
            self.assertEqual(completed.status_code, 204, completed.text)
            me = self.client.get("/api/me").json()
            self.assertEqual((me["email"], me["role"]), (email.lower(), "member"))
            self.assertEqual(self.client.get("/api/users").status_code, 403)
            self.client.post("/api/auth/logout")
            self.fixture.login(email.lower())
            self.client.post("/api/auth/logout")
            self.assertEqual(
                self.client.post(
                    "/api/auth/register/complete",
                    json={
                        "token": token,
                        "password": "attempted-replacement-password",
                    },
                ).status_code,
                400,
            )
        with psycopg.connect(DSN) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM review_agent.admin_users WHERE is_verified"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM review_agent.admin_registration_requests"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM review_agent.admin_audit_events WHERE action = 'account_created' AND details->>'method' = 'email_registration'"
                ).fetchone()[0],
                2,
            )

    def test_closed_disallowed_and_existing_addresses_do_not_receive_links(
        self,
    ) -> None:
        self.assertFalse(self.client.get("/api/auth/registration").json()["enabled"])
        self.assertEqual(
            self.client.post(
                "/api/auth/register", json={"email": "staff@sundsvall.se"}
            ).status_code,
            503,
        )
        self.enable_registration()
        for email in (
            "outside@example.com",
            "name@sub.sundsvall.se",
            "name@sundsvall.se.evil.com",
            "ADMIN@example.com",
        ):
            response = self.client.post("/api/auth/register", json={"email": email})
            self.assertEqual(response.status_code, 202, response.text)
        self.smtp.assert_not_called()
        self.fixture.login()
        self.assertEqual(self.client.get("/api/users").json()["total"], 1)

    def test_verification_rechecks_policy_expiry_and_existing_accounts(self) -> None:
        self.enable_registration()
        self.client.post("/api/auth/register", json={"email": "partner@example.com"})
        token = self.token()
        self.fixture.login()
        response = self.client.put(
            "/api/registration",
            json={
                "expected_revision": 1,
                "enabled": False,
                "allowed_domains": [],
                "allowed_emails": [],
                "reason": "Close registration",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.client.post("/api/auth/logout")
        complete = {"token": token, "password": test_admin_api.PASSWORD}
        self.assertEqual(
            self.client.post("/api/auth/register/complete", json=complete).status_code,
            403,
        )
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE review_agent.admin_registration_requests SET expires_at = now() - interval '1 second'"
            )
        self.assertEqual(
            self.client.post("/api/auth/register/complete", json=complete).status_code,
            400,
        )
        self.fixture.login()
        self.client.put(
            "/api/registration",
            json={
                "expected_revision": 2,
                "enabled": True,
                "allowed_domains": ["sundsvall.se"],
                "reason": "Open staff registration again",
            },
        )
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/register", json={"email": "staff@sundsvall.se"})
        token = self.token()
        self.assertEqual(
            self.client.post(
                "/api/auth/register/complete",
                json={
                    "token": token,
                    "password": "too-short",
                },
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/register/complete",
                json={
                    "token": token,
                    "password": test_admin_api.PASSWORD,
                    "role": "owner",
                },
            ).status_code,
            422,
        )
        self.fixture.login()
        self.assertEqual(
            self.client.post(
                "/api/users",
                json={
                    "email": "staff@sundsvall.se",
                    "password": test_admin_api.PASSWORD,
                },
            ).status_code,
            201,
        )
        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post(
                "/api/auth/register/complete",
                json={
                    "token": token,
                    "password": "attempted-replacement-password",
                },
            ).status_code,
            400,
        )
        self.fixture.login("staff@sundsvall.se")

    def test_delivery_failure_and_resends_preserve_limits_and_replace_old_links(
        self,
    ) -> None:
        self.enable_registration()
        sender = self.smtp.return_value.__enter__.return_value.send_message
        sender.side_effect = smtplib.SMTPException("private mail relay details")
        payload = {"email": "staff@sundsvall.se"}
        failed = self.client.post("/api/auth/register", json=payload)
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertNotIn("private mail relay details", failed.text)
        old_token = self.token()
        self.assertEqual(
            self.client.post("/api/auth/register", json=payload).status_code, 202
        )
        self.assertEqual(sender.call_count, 1)
        sender.side_effect = None
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE review_agent.admin_registration_requests SET requested_at = now() - interval '61 seconds'"
            )
        self.assertEqual(
            self.client.post("/api/auth/register", json=payload).status_code, 202
        )
        token = self.token()
        self.assertNotEqual(token, old_token)
        self.assertEqual(
            self.client.post(
                "/api/auth/register/complete",
                json={
                    "token": old_token,
                    "password": test_admin_api.PASSWORD,
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/register/complete",
                json={
                    "token": token,
                    "password": test_admin_api.PASSWORD,
                },
            ).status_code,
            204,
        )
        with psycopg.connect(DSN) as connection:
            connection.execute("""
                INSERT INTO review_agent.admin_registration_requests
                    (email, token_digest, requested_at, expires_at)
                SELECT 'staff' || value || '@sundsvall.se', lpad(value::text, 64, '0'), now(), now() + interval '30 minutes'
                FROM generate_series(1, 30) AS value
            """)
        busy = self.client.post(
            "/api/auth/register", json={"email": "next@sundsvall.se"}
        )
        self.assertEqual(busy.status_code, 429, busy.text)
        self.assertEqual(busy.headers["retry-after"], "60")
        self.assertEqual(sender.call_count, 2)

    def test_registration_pending_bound_rejects_new_addresses(self) -> None:
        self.enable_registration()
        with psycopg.connect(DSN) as connection:
            connection.execute("""
                INSERT INTO review_agent.admin_registration_requests
                    (email, token_digest, requested_at, expires_at)
                SELECT 'pending-' || value || '@sundsvall.se',
                    lpad(value::text, 64, '0'),
                    now() - interval '2 minutes', now() + interval '28 minutes'
                FROM generate_series(1, 1000) AS value
            """)
        response = self.client.post(
            "/api/auth/register", json={"email": "new@sundsvall.se"}
        )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.headers["Retry-After"], "60")
        self.smtp.assert_not_called()
