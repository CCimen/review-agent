from __future__ import annotations

import os
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import psycopg
import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from tests import test_admin_api


DSN = os.environ.get("REVIEW_AGENT_POSTGRES_DSN", "")
SCIM_TOKEN = "local-scim-test-credential-with-32-characters"
ISSUER = "https://identity.example.test"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@unittest.skipUnless(DSN, "requires an isolated PostgreSQL test database")
class IdentityAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "REVIEW_AGENT_OIDC_ISSUER": ISSUER,
                    "REVIEW_AGENT_OIDC_CLIENT_ID": "review-agent-test",
                    "REVIEW_AGENT_OIDC_CLIENT_SECRET": "local-oidc-test-secret",
                    "REVIEW_AGENT_SCIM_TOKEN": SCIM_TOKEN,
                },
            )
        )
        self.fixture = test_admin_api.AdminAPITests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.client = self.fixture.client

    def mock_provider(self, email: str, subject: str = "directory-subject-123") -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.claim_changes = {}
        self.nonce = ""
        self.token_requests = 0

        async def respond(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/.well-known/openid-configuration":
                return httpx2.Response(
                    200,
                    json={
                        "issuer": ISSUER,
                        "authorization_endpoint": ISSUER + "/authorize",
                        "token_endpoint": ISSUER + "/token",
                        "jwks_uri": ISSUER + "/keys",
                        "id_token_signing_alg_values_supported": ["RS256"],
                    },
                )
            if request.url.path == "/keys":
                return httpx2.Response(
                    200,
                    json={
                        "keys": [
                            json.loads(
                                jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())
                            )
                        ]
                    },
                )
            if request.url.path == "/token":
                self.token_requests += 1
                self.assertIn("code_verifier=", request.content.decode())
                claims = {
                    "iss": ISSUER,
                    "sub": subject,
                    "aud": "review-agent-test",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 300,
                    "nonce": self.nonce,
                    "email": email,
                    "email_verified": True,
                    "roles": ["owner"],
                    **self.claim_changes,
                }
                return httpx2.Response(
                    200,
                    json={
                        "access_token": "local-test-provider-access-token",
                        "token_type": "Bearer",
                        "id_token": jwt.encode(claims, key, algorithm="RS256"),
                    },
                )
            raise AssertionError(f"Unexpected provider request: {request.url.path}")

        self.enterContext(
            patch("httpx2.AsyncHTTPTransport.handle_async_request", side_effect=respond)
        )

    def start_oidc(self, *, link: bool = False) -> str:
        response = self.client.post(
            "/api/account/identity/link" if link else "/api/auth/oidc/start"
        )
        self.assertEqual(response.status_code, 200, response.text)
        query = parse_qs(urlsplit(response.json()["authorization_url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(
            query["redirect_uri"], [test_admin_api.ORIGIN + "/api/auth/oidc/callback"]
        )
        self.nonce = query["nonce"][0]
        return query["state"][0]

    def callback(self, state: str) -> httpx2.Response:
        return self.client.get(
            "/api/auth/oidc/callback",
            params={"state": state, "code": "local-test-authorization-code"},
            follow_redirects=False,
        )

    def test_existing_account_links_deliberately_and_signs_in_by_issuer_subject(
        self,
    ) -> None:
        self.mock_provider("admin@example.com")
        # Matching an existing email alone cannot link a local owner account.
        state = self.start_oidc()
        refused = self.callback(state)
        self.assertEqual(refused.status_code, 303, refused.text)
        self.assertIn("sso_error=account", refused.headers["location"])
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.fixture.login()
        self.claim_changes["email_verified"] = False
        state = self.start_oidc(link=True)
        self.assertEqual(
            self.callback(state).headers["location"], "/account?sso_error=account"
        )
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.claim_changes = {}
        state = self.start_oidc(link=True)
        linked = self.callback(state)
        self.assertEqual(linked.status_code, 303, linked.text)
        self.assertEqual(linked.headers["location"], "/account?identity=linked")
        self.client.post("/api/auth/logout")
        # Email may change at the provider after durable linking.
        self.claim_changes["email"] = "renamed@example.com"
        state = self.start_oidc()
        signed_in = self.callback(state)
        self.assertEqual(signed_in.status_code, 303, signed_in.text)
        self.assertEqual(signed_in.headers["location"], "/")
        self.assertEqual(
            self.client.get("/api/me").json()["email"], "admin@example.com"
        )
        calls = self.token_requests
        self.client.post("/api/auth/logout")
        replay = self.callback(state)
        self.assertIn("sso_error=expired", replay.headers["location"])
        self.assertEqual(self.token_requests, calls)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_scim_account_sign_in_rejects_invalid_claims_and_deactivation(self) -> None:
        headers = {"Authorization": f"Bearer {SCIM_TOKEN}"}
        created = self.client.post(
            "/scim/v2/Users",
            headers=headers,
            json={
                "schemas": [USER_SCHEMA],
                "userName": "sso-member@example.com",
            },
        ).json()
        self.mock_provider("sso-member@example.com")
        for changed_claims in (
            {"nonce": "another-browser-nonce"},
            {"iss": "https://other-issuer.example.test"},
            {"aud": "another-client"},
            {"exp": int(time.time()) - 120},
            {"email_verified": False},
        ):
            with self.subTest(claims=list(changed_claims)):
                self.claim_changes = changed_claims
                state = self.start_oidc()
                response = self.callback(state)
                self.assertEqual(response.status_code, 303, response.text)
                self.assertIn("sso_error=", response.headers["location"])
                self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.claim_changes = {}
        state = self.start_oidc()
        original_cookie = self.client.cookies.get("__Host-review_agent_oidc")
        self.client.cookies.clear()
        self.assertIn("sso_error=expired", self.callback(state).headers["location"])
        self.client.cookies.set("__Host-review_agent_oidc", original_cookie)
        self.assertEqual(self.callback(state).headers["location"], "/")
        self.assertEqual(self.client.get("/api/me").json()["role"], "member")
        state = self.start_oidc()
        self.assertEqual(
            self.client.patch(
                f"/scim/v2/Users/{created['id']}",
                headers=headers,
                json={
                    "schemas": [PATCH_SCHEMA],
                    "Operations": [{"op": "replace", "path": "active", "value": False}],
                },
            ).status_code,
            200,
        )
        self.assertIn("sso_error=account", self.callback(state).headers["location"])
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_scim_provisions_only_member_accounts_and_supports_user_lookup(
        self,
    ) -> None:
        path = "/scim/v2/Users"
        headers = {"Authorization": f"Bearer {SCIM_TOKEN}"}
        self.assertEqual(self.client.get(path).status_code, 401)
        # Provisioners use a bearer credential without a browser Origin header.
        self.client.headers.pop("Origin")
        created = self.client.post(
            path,
            headers=headers,
            json={
                "schemas": [USER_SCHEMA],
                "userName": "member@example.com",
                "externalId": "directory-123",
                "active": True,
                "roles": [{"value": "owner"}],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        resource = created.json()
        self.assertEqual(resource["schemas"], [USER_SCHEMA])
        self.assertEqual(resource["externalId"], "directory-123")
        self.assertTrue(
            created.headers["content-type"].startswith("application/scim+json")
        )
        self.assertEqual(created.headers["location"], resource["meta"]["location"])
        result = self.client.get(
            path, headers=headers, params={"filter": 'userName eq "member@example.com"'}
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["totalResults"], 1)
        self.assertEqual(result.json()["Resources"][0]["id"], resource["id"])
        self.assertEqual(self.client.get("/api/me", headers=headers).status_code, 401)
        self.client.headers["Origin"] = test_admin_api.ORIGIN
        self.fixture.login()
        accounts = self.client.get("/api/users").json()["items"]
        account = next(row for row in accounts if row["id"] == resource["id"])
        self.assertEqual(account["role"], "member")
        duplicate = self.client.post(
            path,
            headers=headers,
            json={"schemas": [USER_SCHEMA], "userName": "admin@example.com"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        self.assertEqual(duplicate.json()["scimType"], "uniqueness")

    def test_scim_deactivation_revokes_sessions_and_preserves_privileged_accounts(
        self,
    ) -> None:
        headers = {"Authorization": f"Bearer {SCIM_TOKEN}"}
        created = self.client.post(
            "/scim/v2/Users",
            headers=headers,
            json={"schemas": [USER_SCHEMA], "userName": "managed@example.com"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        user_id = created.json()["id"]
        path = f"/scim/v2/Users/{user_id}"
        self.fixture.login()
        self.assertEqual(
            self.client.patch(
                f"/api/users/{user_id}", json={"password": test_admin_api.PASSWORD}
            ).status_code,
            200,
        )
        self.fixture.login("managed@example.com")
        revision = self.client.get("/api/me").json()["access_revision"]
        self.client.headers.pop("Origin")
        invalid = self.client.patch(
            path,
            headers=headers,
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {"op": "replace", "path": "active", "value": False},
                    {"op": "replace", "path": "roles", "value": ["owner"]},
                ],
            },
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        invalid_then_replaced = self.client.patch(
            path,
            headers=headers,
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {"op": "replace", "path": "active", "value": "invalid"},
                    {"op": "replace", "path": "active", "value": False},
                ],
            },
        )
        self.assertEqual(
            invalid_then_replaced.status_code, 400, invalid_then_replaced.text
        )
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        disabled = self.client.patch(
            path,
            headers=headers,
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertFalse(disabled.json()["active"])
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        with psycopg.connect(DSN) as connection:
            state = connection.execute(
                "SELECT access_revision, (SELECT count(*) FROM review_agent.admin_sessions WHERE user_id = %s) FROM review_agent.admin_users WHERE id = %s",
                (user_id, user_id),
            ).fetchone()
        self.assertEqual(state, (revision + 1, 0))
        self.assertEqual(
            self.client.patch(
                path,
                headers=headers,
                json={
                    "schemas": [PATCH_SCHEMA],
                    "Operations": [{"op": "replace", "value": {"active": True}}],
                },
            ).status_code,
            200,
        )
        self.client.headers["Origin"] = test_admin_api.ORIGIN
        self.fixture.login()
        owner_id = self.client.get("/api/me").json()["id"]
        self.assertEqual(
            self.client.delete(
                f"/scim/v2/Users/{owner_id}", headers=headers
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/users/{user_id}", json={"role": "admin"}
            ).status_code,
            200,
        )
        protected = self.client.delete(path, headers=headers)
        self.assertEqual(protected.status_code, 403, protected.text)
        self.assertEqual(
            self.client.patch(
                f"/api/users/{user_id}", json={"role": "member"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.delete(path, headers=headers).status_code, 204)
        self.assertEqual(self.client.get(path, headers=headers).status_code, 404)
        self.assertEqual(self.client.get("/api/users").json()["disabled_count"], 1)

    def test_scim_patch_rejects_duplicate_attributes_without_changing_account(
        self,
    ) -> None:
        headers = {"Authorization": f"Bearer {SCIM_TOKEN}"}
        created = self.client.post(
            "/scim/v2/Users",
            headers=headers,
            json={"schemas": [USER_SCHEMA], "userName": "duplicate@example.com"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        path = f"/scim/v2/Users/{created.json()['id']}"
        rejected = self.client.patch(
            path,
            headers=headers,
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [
                    {
                        "op": "replace",
                        "path": "userName",
                        "value": "changed@example.com",
                    },
                    {"op": "replace", "value": {"active": True, "ACTIVE": False}},
                ],
            },
        )
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(rejected.json()["scimType"], "invalidValue")
        self.assertEqual(self.client.get(path, headers=headers).json(), created.json())

    def test_scim_advertises_supported_contract_and_returns_protocol_errors(
        self,
    ) -> None:
        headers = {"Authorization": f"Bearer {SCIM_TOKEN}"}
        config = self.client.get("/scim/v2/ServiceProviderConfig", headers=headers)
        self.assertEqual(config.status_code, 200, config.text)
        self.assertTrue(config.json()["patch"]["supported"])
        self.assertFalse(config.json()["bulk"]["supported"])
        types = self.client.get("/scim/v2/ResourceTypes", headers=headers).json()
        self.assertEqual([row["name"] for row in types["Resources"]], ["User"])
        schema = self.client.get(f"/scim/v2/Schemas/{USER_SCHEMA}", headers=headers)
        self.assertEqual(schema.status_code, 200, schema.text)
        self.assertEqual(schema.json()["id"], USER_SCHEMA)
        created = self.client.post(
            "/scim/v2/Users",
            headers=headers,
            json={
                "schemas": [USER_SCHEMA],
                "userName": "before@example.com",
                "externalId": "directory-456",
            },
        ).json()
        updated = self.client.put(
            f"/scim/v2/Users/{created['id']}",
            headers=headers,
            json={
                "schemas": [USER_SCHEMA],
                "userName": "after@example.com",
                "active": False,
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["userName"], "after@example.com")
        self.assertNotIn("externalId", updated.json())
        self.assertEqual(updated.json()["meta"]["created"], created["meta"]["created"])
        page = self.client.get("/scim/v2/Users?count=0", headers=headers).json()
        self.assertEqual((page["totalResults"], page["Resources"]), (1, []))
        for body in (
            {"schemas": [USER_SCHEMA]},
            {"schemas": [USER_SCHEMA], "userName": "invalid", "active": "false"},
        ):
            invalid = self.client.post("/scim/v2/Users", headers=headers, json=body)
            self.assertEqual(invalid.status_code, 400, invalid.text)
            self.assertEqual(invalid.json()["status"], "400")
            self.assertTrue(
                invalid.headers["content-type"].startswith("application/scim+json")
            )
        oversized = self.client.post(
            "/scim/v2/Users", headers=headers, content=b" " * 65537
        )
        self.assertEqual(oversized.status_code, 413, oversized.text)
