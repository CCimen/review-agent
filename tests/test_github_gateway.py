from __future__ import annotations

import email.message
import io
import json
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import cast
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins"))

from review_agent_tools.github import gateway as gateway_module  # noqa: E402
from review_agent_tools.github import (  # noqa: E402
    publication as publication_module,
    publication_gateway as publication_gateway_module,
)
from review_agent_tools.github.gateway import (  # noqa: E402
    FeedbackAcknowledgementRequest,
    GitHubGatewayProtocolError,
    GitHubGatewayRejected,
    GitHubGatewayRetryable,
    ReviewAcknowledgementRequest,
    ReviewGitHubGateway,
    ReviewSourceRequest,
)
from review_agent_tools.github.gateway_client import (  # noqa: E402
    AuthorizedPublicationGateway,
    ReviewGitHubGatewayClient,
    _error_reason,
)
from review_agent_tools.github.publication import (  # noqa: E402
    GitHubPublicationAuthorityLost,
    GitHubPublicationError,
    GitHubIssueCommentGateway,
    IssueComment,
)
from review_agent_tools.github.publication_gateway import (  # noqa: E402
    PublicationGatewayRequest,
    ReviewPublicationGateway,
)
from review_agent_tools.github.source import (  # noqa: E402
    GitHubSourceError,
    ReviewPullSource,
    read_review_pull,
)
from review_agent_tools.postgres.review_runs import ReviewRunScope  # noqa: E402
from review_agent_tools.source_control import (  # noqa: E402
    GitHubReadError,
    PullSnapshot,
    SameOriginHttpsRedirectHandler,
)


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: email.message.Message | None = None,
    ) -> None:
        self._body = body
        self.status = status
        self.headers = headers or email.message.Message()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


class _RedirectResponse(io.BytesIO):
    def __init__(self, url: str, location: str) -> None:
        super().__init__(b"")
        headers = email.message.Message()
        headers["Location"] = location
        self.code = 302
        self.status = 302
        self.msg = "Found"
        self.headers = headers
        self._url = url

    def info(self) -> email.message.Message:
        return self.headers

    def geturl(self) -> str:
        return self._url


class _RedirectingHttpsTransport(urllib.request.HTTPSHandler):
    def __init__(self, location: str) -> None:
        super().__init__()
        self.location = location
        self.requests: list[urllib.request.Request] = []

    def https_open(self, request: urllib.request.Request) -> _RedirectResponse:
        self.requests.append(request)
        if len(self.requests) > 1:
            raise AssertionError("credentialed redirect performed a second request")
        return _RedirectResponse(request.full_url, self.location)


class _Opener:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[object] = []

    def open(self, request: object, *, timeout: float) -> _Response:
        del timeout
        self.requests.append(request)
        return _Response(self.body, status=self.status)


class ReviewGitHubGatewayClientTests(unittest.TestCase):
    @staticmethod
    def _scope() -> ReviewRunScope:
        return ReviewRunScope(
            run=Mock(),
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            base_sha="b" * 40,
            head_sha="a" * 40,
            resolved_config=Mock(),
        )

    @staticmethod
    def _pull_payload(*, base_id: int = 9001, head_id: int = 9001) -> dict[str, object]:
        return {
            "state": "open",
            "draft": False,
            "title": "Review source cutover",
            "html_url": "https://github.com/CCimen/review-agent/pull/42",
            "user": {"login": "ccimen"},
            "base": {
                "sha": "b" * 40,
                "ref": "main",
                "repo": {"id": base_id, "full_name": "CCimen/review-agent"},
            },
            "head": {
                "sha": "a" * 40,
                "ref": "feature/source-cutover",
                "repo": {"id": head_id, "full_name": "CCimen/review-agent"},
            },
            "changed_files": 3,
            "additions": 20,
            "deletions": 5,
        }

    def test_authorize_review_sends_only_durable_delivery_lease_identity(self) -> None:
        opener = _Opener(
            json.dumps(
                {
                    "provider_installation_id": 7001,
                    "provider_repository_id": 9001,
                    "repository": "CCimen/review-agent",
                    "pr_number": 42,
                    "comment_id": 6001,
                    "sender_login": "ccimen",
                    "base_sha": "b" * 40,
                    "head_sha": "a" * 40,
                }
            ).encode("utf-8")
        )
        client = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646",
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        result = client.authorize_review_delivery(
            delivery_id=31,
            lease_owner="github-app:worker-1",
            lease_generation=4,
        )

        self.assertEqual(result.provider_repository_id, 9001)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(
            getattr(request, "full_url"),
            "http://review-github-gateway:8646/v1/review-deliveries/authorize",
        )
        self.assertEqual(
            json.loads(getattr(request, "data")),
            {
                "delivery_id": 31,
                "lease_owner": "github-app:worker-1",
                "lease_generation": 4,
            },
        )

    def test_feedback_gateway_sends_only_delivery_lease_and_fixed_status(self) -> None:
        authorize_opener = _Opener(
            json.dumps(
                {
                    "provider_installation_id": 7001,
                    "provider_repository_id": 9001,
                    "repository": "CCimen/review-agent",
                    "pr_number": 42,
                    "comment_id": 6001,
                    "sender_id": 5001,
                    "sender_login": "ccimen",
                    "author_association": "MEMBER",
                    "authorization_version": "sha256:" + ("a" * 64),
                }
            ).encode("utf-8")
        )
        client = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646",
            opener=cast(urllib.request.OpenerDirector, authorize_opener),
        )

        result = client.authorize_feedback_delivery(
            delivery_id=32,
            lease_owner="github-app:worker-1",
            lease_generation=5,
        )

        self.assertEqual(result.sender_id, 5001)
        request = authorize_opener.requests[0]
        self.assertEqual(
            getattr(request, "full_url"),
            "http://review-github-gateway:8646/v1/review-feedback/authorize",
        )
        self.assertEqual(
            json.loads(getattr(request, "data")),
            {
                "delivery_id": 32,
                "lease_owner": "github-app:worker-1",
                "lease_generation": 5,
            },
        )

        acknowledgement_opener = _Opener(b'{"acknowledged":true}')
        acknowledgement_client = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646",
            opener=cast(urllib.request.OpenerDirector, acknowledgement_opener),
        )
        acknowledged = acknowledgement_client.acknowledge_feedback(
            delivery_id=32,
            lease_owner="github-app:worker-1",
            lease_generation=5,
            status="recorded",
        )
        self.assertTrue(acknowledged)
        acknowledgement_request = acknowledgement_opener.requests[0]
        self.assertEqual(
            getattr(acknowledgement_request, "full_url"),
            "http://review-github-gateway:8646/v1/review-feedback/acknowledge",
        )
        self.assertEqual(
            json.loads(getattr(acknowledgement_request, "data")),
            {
                "delivery_id": 32,
                "lease_owner": "github-app:worker-1",
                "lease_generation": 5,
                "status": "recorded",
            },
        )

    def test_review_acknowledgement_sends_only_admitted_run_identity(self) -> None:
        opener = _Opener(b'{"acknowledged":true}')
        client = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646",
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        acknowledged = client.acknowledge_review(run_id=51)

        self.assertTrue(acknowledged)
        request = opener.requests[0]
        self.assertEqual(
            getattr(request, "full_url"),
            "http://review-github-gateway:8646/v1/review-runs/acknowledge",
        )
        self.assertEqual(
            json.loads(getattr(request, "data")),
            {"run_id": 51},
        )
    def test_publication_gateway_preserves_failure_classification(self) -> None:
        client = Mock()
        gateway = AuthorizedPublicationGateway(
            client,
            scope_kind="publication",
            scope_id=71,
            lease_owner="publisher-1",
            lease_generation=3,
        )
        client.execute_publication_operation.side_effect = GitHubGatewayRejected(
            "repository_not_authorized"
        )

        with self.assertRaises(GitHubPublicationError) as rejected:
            gateway.current_user_login()

        self.assertFalse(rejected.exception.retryable)
        self.assertEqual(rejected.exception.code, "repository_not_authorized")

        client.execute_publication_operation.side_effect = GitHubGatewayRejected(
            "publication_lease_lost"
        )
        with self.assertRaises(GitHubPublicationAuthorityLost):
            gateway.current_user_login()

    def test_publication_gateway_rejects_caller_subject_drift(self) -> None:
        client = Mock()
        client.execute_publication_operation.return_value = {
            "kind": "pull",
            "state": "open",
            "draft": False,
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
        }
        gateway = AuthorizedPublicationGateway(
            client,
            scope_kind="publication",
            scope_id=71,
            lease_owner="publisher-1",
            lease_generation=3,
        )

        gateway.get_pull_request("CCimen/review-agent", 42)
        with self.assertRaisesRegex(
            GitHubPublicationError, "publication_subject_mismatch"
        ):
            gateway.get_pull_request("CCimen/other", 42)

        client.execute_publication_operation.assert_called_once()

    def test_provider_rejects_comment_without_positive_id(self) -> None:
        response = _Response(b'[{"body":"text","user":{"login":"bot"}}]')
        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.return_value = response
        gateway = GitHubIssueCommentGateway(
            "installation-token",
            max_attempts=1,
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        with self.assertRaisesRegex(
            GitHubPublicationError, "github_bad_comments_response"
        ):
            gateway.list_issue_comments("CCimen/review-agent", 42)

    def test_newest_issue_comment_scan_reads_tail_pages_within_budget(self) -> None:
        def page(*comment_ids: int) -> bytes:
            return json.dumps(
                [
                    {
                        "id": comment_id,
                        "body": f"comment {comment_id}",
                        "user": {"login": "review-agent[bot]"},
                    }
                    for comment_id in comment_ids
                ]
            ).encode("utf-8")

        link_headers = email.message.Message()
        link_headers["Link"] = (
            '<https://api.github.com/repos/CCimen/review-agent/issues/42/comments'
            '?per_page=100&page=2>; rel="next", '
            '<https://api.github.com/repos/CCimen/review-agent/issues/42/comments'
            '?per_page=100&page=20>; rel="last"'
        )
        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.side_effect = (
            _Response(page(*range(1, 101)), headers=link_headers),
            _Response(page(1901, 1902)),
            _Response(page(1801, 1802)),
        )
        gateway = GitHubIssueCommentGateway(
            "installation-token",
            max_attempts=1,
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        comments = gateway.list_issue_comments(
            "CCimen/review-agent",
            42,
            max_pages=3,
            newest_first=True,
        )

        self.assertEqual(
            [comment.comment_id for comment in comments],
            [1902, 1901, 1802, 1801],
        )
        requests = [item.args[0] for item in opener.open.call_args_list]
        self.assertEqual(len(requests), 3)
        urls = [request.full_url for request in requests]
        self.assertIn("page=1", urls[0])
        self.assertIn("page=20", urls[1])
        self.assertIn("page=19", urls[2])
        self.assertTrue(all("sort=" not in url for url in urls))
        self.assertTrue(all("direction=" not in url for url in urls))

    def test_feedback_reaction_reports_only_new_creation(self) -> None:
        for status, expected in ((201, True), (200, False)):
            opener = _Opener(b'{"id":1}', status=status)
            gateway = GitHubIssueCommentGateway(
                "installation-token",
                max_attempts=1,
                opener=cast(urllib.request.OpenerDirector, opener),
            )
            with self.subTest(status=status):
                created = gateway.create_issue_comment_reaction(
                    "CCimen/review-agent", 6001, "confused"
                )
                self.assertEqual(created, expected)

    def test_publication_transport_classifies_provider_failures(self) -> None:
        cases = (
            (400, {}, b"", False),
            (403, {}, b"", False),
            (403, {"Retry-After": "30"}, b"", True),
            (403, {}, b'{"message":"secondary rate limit"}', True),
            (408, {}, b"", True),
            (425, {}, b"", True),
            (429, {}, b"", True),
            (409, {}, b"", False),
            (422, {}, b"", False),
            (500, {}, b"", True),
        )
        for status, raw_headers, body, expected_retryable in cases:
            headers = email.message.Message()
            for name, value in raw_headers.items():
                headers[name] = value
            provider_error = urllib.error.HTTPError(
                "https://api.github.com/repos/example/project/pulls/1",
                status,
                "provider failure",
                headers,
                io.BytesIO(body),
            )
            opener = Mock(spec=urllib.request.OpenerDirector)
            opener.open.side_effect = provider_error
            gateway = GitHubIssueCommentGateway(
                "installation-token",
                max_attempts=1,
                opener=cast(urllib.request.OpenerDirector, opener),
            )

            with self.subTest(status=status, headers=raw_headers, body=body):
                with self.assertRaises(GitHubPublicationError) as raised:
                    gateway.get_pull_request("example/project", 1)
                self.assertEqual(raised.exception.retryable, expected_retryable)
                self.assertEqual(opener.open.call_count, 1)
                self.assertTrue(provider_error.closed)
                self.assertNotIn("installation-token", str(raised.exception))

    def test_publication_redirect_does_not_forward_the_token(self) -> None:
        transport = _RedirectingHttpsTransport("https://redirect.test/pulls/1")
        opener = urllib.request.build_opener(
            SameOriginHttpsRedirectHandler(), transport
        )
        gateway = GitHubIssueCommentGateway(
            "installation-token",
            max_attempts=1,
            opener=opener,
        )

        with self.assertRaises(GitHubPublicationError):
            gateway.get_pull_request("example/project", 1)

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(
            transport.requests[0].get_header("Authorization"),
            "Bearer installation-token",
        )

    def test_publication_network_failure_is_retryable(self) -> None:
        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.side_effect = urllib.error.URLError("offline")
        gateway = GitHubIssueCommentGateway(
            "installation-token",
            max_attempts=1,
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        with self.assertRaises(GitHubPublicationError) as raised:
            gateway.get_pull_request("example/project", 1)

        self.assertTrue(raised.exception.retryable)

    def test_publication_rate_limit_waits_for_the_durable_retry(self) -> None:
        headers = email.message.Message()
        headers["Retry-After"] = "30"
        provider_error = urllib.error.HTTPError(
            "https://api.github.com/repos/example/project/pulls/1",
            403,
            "rate limited",
            headers,
            io.BytesIO(b""),
        )
        opener = Mock(spec=urllib.request.OpenerDirector)
        opener.open.side_effect = provider_error
        gateway = GitHubIssueCommentGateway(
            "installation-token",
            max_attempts=3,
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        with self.assertRaises(GitHubPublicationError) as raised:
            gateway.get_pull_request("example/project", 1)

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(opener.open.call_count, 1)

    def test_credentialed_clients_install_the_redirect_policy(self) -> None:
        publication = GitHubIssueCommentGateway("installation-token")
        gateway = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646", operator_key="operator-secret"
        )

        for opener in (publication._opener, gateway._opener):
            self.assertTrue(
                any(
                    type(handler) is SameOriginHttpsRedirectHandler
                    for handler in opener.handlers
                )
            )

        handler = SameOriginHttpsRedirectHandler()
        request = urllib.request.Request(
            "http://review-github-gateway:8646/v1/status",
            headers={"Authorization": "Bearer operator-secret"},
        )
        self.assertIsNone(
            handler.redirect_request(
                request,
                io.BytesIO(),
                307,
                "temporary redirect",
                email.message.Message(),
                "http://other-service:8646/v1/status",
            )
        )

    def test_failure_status_gateway_rejects_scan_above_page_contract(self) -> None:
        request = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "failure_status",
                "scope_id": 51,
                "lease_owner": "publisher-1",
                "lease_generation": 3,
                "operation": "list_issue_comments",
                "max_pages": publication_module.PUBLICATION_REQUEST_MAX_PAGES,
                "newest_first": True,
            }
        )
        self.assertEqual(
            request.max_pages, publication_module.PUBLICATION_REQUEST_MAX_PAGES
        )
        with self.assertRaises(GitHubGatewayProtocolError):
            PublicationGatewayRequest.from_mapping(
                {
                    "scope_kind": "failure_status",
                    "scope_id": 51,
                    "lease_owner": "publisher-1",
                    "lease_generation": 3,
                    "operation": "list_issue_comments",
                    "max_pages": (
                        publication_module.PUBLICATION_REQUEST_MAX_PAGES + 1
                    ),
                    "newest_first": True,
                }
            )

    def test_source_contract_rejects_caller_selected_repository_or_revision(self) -> None:
        request = {
            "operation": "pull",
            "run_id": 51,
            "job_id": 61,
            "lease_generation": 7,
        }
        for field, value in (
            ("repository", "other/repository"),
            ("head_sha", "a" * 40),
            ("token", "secret"),
        ):
            with self.subTest(field=field), self.assertRaises(
                GitHubGatewayProtocolError
            ):
                ReviewSourceRequest.from_mapping({**request, field: value})

    def test_publication_contract_rejects_caller_selected_authority(self) -> None:
        request = {
            "scope_kind": "publication",
            "scope_id": 71,
            "lease_owner": "publisher-1",
            "lease_generation": 3,
            "operation": "current_user",
        }
        for field, value in (
            ("repository", "other/repository"),
            ("pr_number", 99),
            ("token", "secret"),
            ("url", "https://example.invalid"),
        ):
            with self.subTest(field=field), self.assertRaises(
                GitHubGatewayProtocolError
            ):
                PublicationGatewayRequest.from_mapping({**request, field: value})

        posted_request = {
            "scope_kind": "posted_publication",
            "scope_id": 71,
            "operation": "current_user",
        }
        with self.assertRaises(GitHubGatewayProtocolError):
            PublicationGatewayRequest.from_mapping(
                {**posted_request, "lease_owner": "publisher-1"}
            )

        feedback_request = {
            "delivery_id": 32,
            "lease_owner": "worker-feedback",
            "lease_generation": 5,
            "status": "recorded",
        }
        for field, value in (
            ("repository", "other/repository"),
            ("comment_id", 999),
            ("token", "secret"),
            ("body", "caller-controlled"),
        ):
            with self.subTest(field=field), self.assertRaises(
                GitHubGatewayProtocolError
            ):
                FeedbackAcknowledgementRequest.from_mapping(
                    {**feedback_request, field: value}
                )

        review_request = {"run_id": 51}
        for field, value in (
            ("repository", "other/repository"),
            ("comment_id", 999),
            ("token", "secret"),
        ):
            with self.subTest(field=field), self.assertRaises(
                GitHubGatewayProtocolError
            ):
                ReviewAcknowledgementRequest.from_mapping(
                    {**review_request, field: value}
                )

    def test_review_ack_rechecks_run_and_uses_write_scoped_token(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        github.create_issue_comment_reaction.return_value = True
        factory = Mock(return_value=github)
        service = ReviewGitHubGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            feedback_factory=factory,
        )
        target = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            comment_id=6001,
        )

        with patch.object(
            ReviewGitHubGateway,
            "_require_review_acknowledgement",
            side_effect=(target, target),
        ) as authorize:
            acknowledged = service.acknowledge_review(run_id=51)

        self.assertTrue(acknowledged)
        self.assertEqual(authorize.call_count, 2)
        tokens.token_for.assert_called_once_with(9001, purpose="publication")
        factory.assert_called_once_with("installation-token")
        github.create_issue_comment_reaction.assert_called_once_with(
            "CCimen/review-agent", 6001, "eyes"
        )

    def test_feedback_ack_rechecks_lease_and_uses_write_scoped_token(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        github.create_issue_comment_reaction.return_value = True
        factory = Mock(return_value=github)
        service = ReviewGitHubGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            feedback_factory=factory,
        )
        command = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            comment_id=6001,
        )

        with patch.object(
            ReviewGitHubGateway,
            "_require_authority",
            side_effect=(command, command),
        ) as authorize:
            acknowledged = service.acknowledge_feedback(
                delivery_id=32,
                lease_owner="worker-feedback",
                lease_generation=5,
                status="recorded",
            )

        self.assertTrue(acknowledged)
        self.assertEqual(authorize.call_count, 2)
        tokens.token_for.assert_called_once_with(9001, purpose="publication")
        factory.assert_called_once_with("installation-token")
        github.create_issue_comment_reaction.assert_called_once_with(
            "CCimen/review-agent", 6001, "+1"
        )
        github.create_issue_comment.assert_not_called()

    def test_stale_intentional_feedback_gets_a_deterministic_retry_message(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        github.create_issue_comment.return_value = IssueComment(
            7001,
            "stale",
            "review-agent[bot]",
        )
        github.list_issue_comments.return_value = []
        github.create_issue_comment_reaction.return_value = True
        service = ReviewGitHubGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            feedback_factory=Mock(return_value=github),
        )
        command = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            comment_id=6001,
        )

        with patch.object(
            ReviewGitHubGateway,
            "_require_authority",
            side_effect=(command, command, command),
        ):
            acknowledged = service.acknowledge_feedback(
                delivery_id=32,
                lease_owner="worker-feedback",
                lease_generation=5,
                status="stale",
            )

        self.assertTrue(acknowledged)
        github.create_issue_comment_reaction.assert_called_once_with(
            "CCimen/review-agent", 6001, "confused"
        )
        body = github.create_issue_comment.call_args.args[2]
        self.assertIn("exact accepted ADR snapshot and path", body)
        self.assertIn("Run `/review`", body)

    def test_feedback_ack_recovers_a_comment_after_an_ambiguous_write(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        tokens.app_bot_login.return_value = "review-agent[bot]"
        github = Mock()
        marker = "<!-- review-agent:feedback-ack source-comment=6001 -->"
        github.list_issue_comments.side_effect = (
            [],
            [IssueComment(7001, f"status\n\n{marker}", "review-agent[bot]")],
        )
        github.create_issue_comment.side_effect = GitHubPublicationError(
            "github_unreachable", status=503, retryable=True
        )
        github.create_issue_comment_reaction.return_value = True
        service = ReviewGitHubGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            feedback_factory=Mock(return_value=github),
        )
        command = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
            comment_id=6001,
        )

        with patch.object(
            ReviewGitHubGateway,
            "_require_authority",
            side_effect=(command, command, command, command),
        ):
            with self.assertRaises(GitHubGatewayRetryable):
                service.acknowledge_feedback(
                    delivery_id=32,
                    lease_owner="worker-feedback",
                    lease_generation=5,
                    status="no_mapping",
                )
            acknowledged = service.acknowledge_feedback(
                delivery_id=32,
                lease_owner="worker-feedback",
                lease_generation=5,
                status="no_mapping",
            )

        self.assertTrue(acknowledged)
        self.assertEqual(github.create_issue_comment.call_count, 1)
        tokens.app_bot_login.assert_called()
        github.current_user_login.assert_not_called()
        github.create_issue_comment_reaction.assert_called_once_with(
            "CCimen/review-agent", 6001, "confused"
        )

    def test_posted_publication_rejects_new_provider_writes(self) -> None:
        runtime = Mock()
        runtime.transaction.return_value = nullcontext(Mock())
        tokens = Mock()
        service = ReviewPublicationGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
        )
        request = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "posted_publication",
                "scope_id": 71,
                "operation": "create_issue_comment",
                "body": "must not be accepted",
            }
        )

        with self.assertRaises(GitHubGatewayRejected) as raised:
            service.execute(request)

        self.assertEqual(
            raised.exception.reason, "publication_operation_not_allowed"
        )
        tokens.token_for.assert_not_called()

    def test_publication_rechecks_lease_and_uses_write_scoped_token(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        github.create_issue_comment.return_value = IssueComment(
            comment_id=81,
            body="published",
            author_login="review-agent[bot]",
        )
        factory = Mock(return_value=github)
        service = ReviewPublicationGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            github_factory=factory,
        )
        scope = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
        )
        request = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "publication",
                "scope_id": 71,
                "lease_owner": "publisher-1",
                "lease_generation": 3,
                "operation": "create_issue_comment",
                "body": "published",
            }
        )

        with patch.object(
            ReviewPublicationGateway,
            "_require_authority",
            side_effect=(scope, scope),
        ) as authorize:
            result = service.execute(request)

        self.assertEqual(result, github.create_issue_comment.return_value)
        self.assertEqual(authorize.call_count, 2)
        tokens.token_for.assert_called_once_with(9001, purpose="publication")
        factory.assert_called_once_with("installation-token")
        github.create_issue_comment.assert_called_once_with(
            "CCimen/review-agent", 42, "published"
        )

    def test_publication_retries_one_invalid_token_only_once(self) -> None:
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        github.create_issue_comment.side_effect = (
            GitHubPublicationError(
                "github_401_create_issue_comment",
                status=401,
                retryable=False,
            ),
            GitHubPublicationError(
                "github_401_create_issue_comment",
                status=401,
                retryable=False,
            ),
        )
        service = ReviewPublicationGateway(
            postgres=Mock(),
            tokens=tokens,
            profile="default-standard",
            github_factory=Mock(return_value=github),
        )
        scope = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
        )
        request = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "publication",
                "scope_id": 71,
                "lease_owner": "publisher-1",
                "lease_generation": 3,
                "operation": "create_issue_comment",
                "body": "published",
            }
        )

        with (
            patch.object(
                ReviewPublicationGateway, "_require_authority", return_value=scope
            ),
            self.assertRaises(GitHubGatewayRejected),
        ):
            service.execute(request)

        self.assertEqual(tokens.token_for.call_count, 2)
        tokens.invalidate.assert_called_once_with(9001, purpose="publication")

    def test_publication_gateway_preserves_transport_retry_classification(self) -> None:
        for retryable, expected in (
            (True, GitHubGatewayRetryable),
            (False, GitHubGatewayRejected),
        ):
            tokens = Mock()
            tokens.token_for.return_value = SimpleNamespace(
                value="installation-token"
            )
            github = Mock()
            github.get_pull_request.side_effect = GitHubPublicationError(
                "github_http_503_get_pull_request" if retryable else "github_http_422_get_pull_request",
                status=503 if retryable else 422,
                retryable=retryable,
            )
            service = ReviewPublicationGateway(
                postgres=Mock(),
                tokens=tokens,
                profile="default-standard",
                github_factory=Mock(return_value=github),
            )
            scope = SimpleNamespace(
                provider_repository_id=9001,
                repository="CCimen/review-agent",
                pr_number=42,
            )
            request = PublicationGatewayRequest.from_mapping(
                {
                    "scope_kind": "publication",
                    "scope_id": 71,
                    "lease_owner": "publisher-1",
                    "lease_generation": 3,
                    "operation": "get_pull",
                }
            )

            with (
                self.subTest(retryable=retryable),
                patch.object(
                    ReviewPublicationGateway,
                    "_require_authority",
                    return_value=scope,
                ),
                self.assertRaises(expected),
            ):
                service.execute(request)

    def test_publication_provider_call_fits_inside_client_deadline(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        github.get_pull_request.return_value = Mock()
        service = ReviewPublicationGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
        )
        scope = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
        )
        request = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "publication",
                "scope_id": 71,
                "lease_owner": "publisher-1",
                "lease_generation": 3,
                "operation": "get_pull",
            }
        )

        with (
            patch.object(
                ReviewPublicationGateway,
                "_require_authority",
                side_effect=(scope, scope),
            ),
            patch.object(
                publication_gateway_module,
                "GitHubIssueCommentGateway",
                return_value=github,
            ) as gateway_class,
        ):
            service.execute(request)

        gateway_class.assert_called_once_with(
            "installation-token",
            request_timeout_seconds=10.0,
            max_attempts=1,
        )

    def test_publication_identity_uses_app_jwt_not_installation_token(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.app_bot_login.return_value = "review-agent[bot]"
        factory = Mock()
        service = ReviewPublicationGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            github_factory=factory,
        )
        scope = SimpleNamespace(
            provider_repository_id=9001,
            repository="CCimen/review-agent",
            pr_number=42,
        )
        request = PublicationGatewayRequest.from_mapping(
            {
                "scope_kind": "publication",
                "scope_id": 71,
                "lease_owner": "publisher-1",
                "lease_generation": 3,
                "operation": "current_user",
            }
        )

        with patch.object(
            ReviewPublicationGateway,
            "_require_authority",
            side_effect=(scope, scope),
        ) as authorize:
            result = service.execute(request)

        self.assertEqual(result, "review-agent[bot]")
        self.assertEqual(authorize.call_count, 2)
        tokens.app_bot_login.assert_called_once_with()
        tokens.token_for.assert_not_called()
        factory.assert_not_called()

    def test_source_read_rechecks_authority_after_provider_io(self) -> None:
        runtime = Mock()
        runtime.transaction.return_value = nullcontext(Mock())
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        github = Mock()
        service = ReviewGitHubGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            github_factory=Mock(return_value=github),
        )
        scope = self._scope()
        result = ReviewPullSource(
            repository="CCimen/review-agent",
            pr_number=42,
            payload={"state": "open"},
        )
        request = ReviewSourceRequest.from_mapping(
            {
                "operation": "pull",
                "run_id": 51,
                "job_id": 61,
                "lease_generation": 7,
            }
        )

        with (
            patch.object(
                ReviewGitHubGateway,
                "_require_source_authority",
                side_effect=(scope, GitHubGatewayRejected("review_job_lease_lost")),
            ) as authorize,
            patch.object(
                gateway_module,
                "read_review_pull",
                return_value=result,
            ) as read,
            self.assertRaisesRegex(
                GitHubGatewayRejected, "review_job_lease_lost"
            ),
        ):
            service.read_review_source(request)

        self.assertEqual(authorize.call_count, 2)
        read.assert_called_once_with(github, scope)

    def test_source_gateway_preserves_transport_retry_classification(self) -> None:
        request = ReviewSourceRequest.from_mapping(
            {
                "operation": "pull",
                "run_id": 51,
                "job_id": 61,
                "lease_generation": 7,
            }
        )
        for retryable, expected in (
            (True, GitHubGatewayRetryable),
            (False, GitHubGatewayRejected),
        ):
            tokens = Mock()
            tokens.token_for.return_value = SimpleNamespace(value="installation-token")
            service = ReviewGitHubGateway(
                postgres=Mock(),
                tokens=tokens,
                profile="default-standard",
                github_factory=Mock(return_value=Mock()),
            )

            with (
                self.subTest(retryable=retryable),
                patch.object(
                    ReviewGitHubGateway,
                    "_require_source_authority",
                    return_value=self._scope(),
                ),
                patch.object(
                    gateway_module,
                    "read_review_pull",
                    side_effect=GitHubReadError(
                        "http_error",
                        "provider failure",
                        status=503 if retryable else 422,
                        retryable=retryable,
                    ),
                ),
                self.assertRaises(expected),
            ):
                service.read_review_source(request)

    def test_source_retries_one_invalid_token_only_once(self) -> None:
        tokens = Mock()
        tokens.token_for.return_value = SimpleNamespace(value="installation-token")
        service = ReviewGitHubGateway(
            postgres=Mock(),
            tokens=tokens,
            profile="default-standard",
            github_factory=Mock(return_value=Mock()),
        )
        request = ReviewSourceRequest.from_mapping(
            {
                "operation": "pull",
                "run_id": 51,
                "job_id": 61,
                "lease_generation": 7,
            }
        )

        with (
            patch.object(
                ReviewGitHubGateway,
                "_require_source_authority",
                return_value=self._scope(),
            ),
            patch.object(
                gateway_module,
                "read_review_pull",
                side_effect=(
                    GitHubReadError("unauthorized", "invalid token", status=401),
                    GitHubReadError("unauthorized", "invalid token", status=401),
                ),
            ),
            self.assertRaises(GitHubGatewayRejected),
        ):
            service.read_review_source(request)

        self.assertEqual(tokens.token_for.call_count, 2)
        tokens.invalidate.assert_called_once_with(9001)

    def test_operator_smoke_reads_one_enabled_pull_and_proves_write_scope(self) -> None:
        runtime = Mock()
        tokens = Mock()
        tokens.token_for.side_effect = (
            SimpleNamespace(value="read-token"),
            SimpleNamespace(value="publication-token"),
        )
        github = Mock()
        factory = Mock(return_value=github)
        service = ReviewGitHubGateway(
            postgres=runtime,
            tokens=tokens,
            profile="default-standard",
            github_factory=factory,
        )
        access = SimpleNamespace(
            provider_repository_id=9001,
            full_name="CCimen/review-agent",
        )
        snapshot = PullSnapshot(
            repository_id=9001,
            repository="CCimen/review-agent",
            number=42,
            state="open",
            base_sha="b" * 40,
            head_sha="a" * 40,
            head_repository_id=9001,
            head_repository="CCimen/review-agent",
        )

        with (
            patch.object(
                ReviewGitHubGateway,
                "_require_operator_repository",
                side_effect=(access, access),
            ) as authorize,
            patch.object(
                gateway_module, "read_pull_snapshot", return_value=snapshot
            ) as read,
        ):
            result = service.operator_smoke(
                repository="CCimen/review-agent", pr_number=42
            )

        self.assertEqual(authorize.call_count, 2)
        self.assertEqual(result.repository_id, 9001)
        self.assertEqual(result.base_sha, "b" * 40)
        self.assertEqual(result.head_sha, "a" * 40)
        self.assertTrue(result.publication_permission)
        self.assertEqual(
            tokens.token_for.call_args_list,
            [
                call(9001),
                call(9001, purpose="publication"),
            ],
        )
        factory.assert_called_once_with("read-token")
        read.assert_called_once_with(github, "CCimen/review-agent", 42)
        for method in (
            "create_issue_comment",
            "create_pull_request_review",
            "update_issue_comment",
        ):
            getattr(github, method).assert_not_called()

    def test_pull_source_preserves_stable_repository_identity(self) -> None:
        github = Mock()
        github.request_json.return_value = self._pull_payload()

        result = read_review_pull(github, self._scope())

        self.assertEqual(result.repository, "CCimen/review-agent")
        self.assertEqual(result.payload["base"]["repo"]["id"], 9001)

    def test_pull_source_rejects_repository_identity_change_or_fork(self) -> None:
        for payload, message in (
            (self._pull_payload(base_id=9002), "repository identity changed"),
            (self._pull_payload(head_id=9002), "fork source is not supported"),
        ):
            with self.subTest(message=message):
                github = Mock()
                github.request_json.return_value = payload
                with self.assertRaisesRegex(GitHubSourceError, message):
                    read_review_pull(github, self._scope())

    def test_invalid_remote_reason_uses_database_safe_protocol_code(self) -> None:
        self.assertEqual(
            _error_reason(b'{"reason":"Not Found"}'),
            "github_gateway_invalid_response",
        )

    def test_pull_source_read_sends_only_run_and_worker_lease_identity(self) -> None:
        opener = _Opener(
            json.dumps(
                {
                    "kind": "pull",
                    "repository": "CCimen/review-agent",
                    "pr_number": 42,
                    "payload": {"state": "open"},
                }
            ).encode("utf-8")
        )
        client = ReviewGitHubGatewayClient(
            "http://review-github-gateway:8646",
            opener=cast(urllib.request.OpenerDirector, opener),
        )

        result = client.get_review_pull(
            run_id=51,
            job_id=61,
            lease_generation=7,
        )

        self.assertEqual(result.repository, "CCimen/review-agent")
        request = opener.requests[0]
        self.assertEqual(
            getattr(request, "full_url"),
            "http://review-github-gateway:8646/v1/review-sources/read",
        )
        self.assertEqual(
            json.loads(getattr(request, "data")),
            {
                "operation": "pull",
                "run_id": 51,
                "job_id": 61,
                "lease_generation": 7,
            },
        )


if __name__ == "__main__":
    unittest.main()
