from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import cast
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from review_agent_tools.github import app_worker  # noqa: E402
from review_agent_tools.github.app_processor import GitHubAppProcessor  # noqa: E402
from review_agent_tools.postgres.runtime import PostgreSQLRuntime  # noqa: E402


class _Runtime:
    def __init__(self) -> None:
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield object()


class _Processor:
    def __init__(self, result: object | None = None) -> None:
        self.result = result
        self.owners: list[str] = []

    def process_next(self, *, lease_owner: str) -> object | None:
        self.owners.append(lease_owner)
        return self.result


def _entrypoint_module():
    spec = importlib.util.spec_from_file_location(
        "review_agent_github_app_worker_entrypoint",
        ROOT / "tools" / "review_agent_github_app_worker.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load GitHub App worker entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GitHubAppWorkerTests(unittest.TestCase):
    def test_once_recovers_one_bounded_batch_then_processes_one_delivery(self) -> None:
        runtime = _Runtime()
        processor = _Processor()
        worker = app_worker.GitHubAppWorker(
            cast(PostgreSQLRuntime, runtime),
            cast(GitHubAppProcessor, processor),
            app_worker.GitHubAppWorkerPolicy(recovery_batch_size=7),
            lease_owner="github-app:test",
            stop_event=threading.Event(),
        )

        with patch.object(
            app_worker.webhook_deliveries, "recover_expired_deliveries"
        ) as recover:
            worker.run(once=True)

        recover.assert_called_once_with(
            unittest.mock.ANY, limit=7, actor="github-app:test"
        )
        self.assertEqual(processor.owners, ["github-app:test"])
        self.assertEqual(runtime.transactions, 1)

    def test_unexpected_processor_defect_propagates_without_guessed_transition(self) -> None:
        class DefectProcessor(_Processor):
            def process_next(self, *, lease_owner: str) -> object | None:
                del lease_owner
                raise RuntimeError("defect")

        worker = app_worker.GitHubAppWorker(
            cast(PostgreSQLRuntime, _Runtime()),
            cast(GitHubAppProcessor, DefectProcessor()),
            app_worker.GitHubAppWorkerPolicy(),
            lease_owner="github-app:test",
            stop_event=threading.Event(),
        )
        with (
            patch.object(app_worker.webhook_deliveries, "recover_expired_deliveries"),
            self.assertRaisesRegex(RuntimeError, "defect"),
        ):
            worker.run(once=True)

    def test_policy_rejects_unbounded_or_tight_loop_configuration(self) -> None:
        for arguments in (
            {"poll_interval": timedelta(0)},
            {"recovery_batch_size": 0},
            {
                "database_backoff_initial": timedelta(seconds=2),
                "database_backoff_maximum": timedelta(seconds=1),
            },
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(app_worker.GitHubAppWorkerConfigurationError):
                    app_worker.GitHubAppWorkerPolicy(**arguments)


class GitHubAppWorkerEntrypointTests(unittest.TestCase):
    def test_main_enables_operator_visible_info_logs(self) -> None:
        module = _entrypoint_module()
        with (
            patch.object(module.logging, "basicConfig") as basic_config,
            patch.object(
                module.ReviewAgentSettings,
                "from_environment",
                side_effect=RuntimeError("stop after logging setup"),
            ),
            self.assertRaisesRegex(RuntimeError, "stop after logging setup"),
        ):
            module.main(["--once"])

        basic_config.assert_called_once_with(
            level=logging.INFO,
            stream=sys.stdout,
            format="%(levelname)s %(name)s %(message)s",
        )

    def test_private_key_loader_accepts_only_bounded_regular_utf8_file(self) -> None:
        module = _entrypoint_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = root / "app.pem"
            key.write_text("private-key-pem", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": str(key)},
                clear=False,
            ):
                self.assertEqual(module._private_key(), "private-key-pem")

            link = root / "key-link.pem"
            link.symlink_to(key)
            with patch.dict(
                os.environ,
                {"REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE": str(link)},
                clear=False,
            ):
                with self.assertRaises(
                    app_worker.GitHubAppWorkerConfigurationError
                ):
                    module._private_key()

    def test_app_id_is_strictly_positive(self) -> None:
        module = _entrypoint_module()
        for value in ("", "not-an-integer", "0", "-1"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"REVIEW_AGENT_GITHUB_APP_ID": value}, clear=False
            ):
                with self.assertRaises(
                    app_worker.GitHubAppWorkerConfigurationError
                ):
                    module._positive_integer("REVIEW_AGENT_GITHUB_APP_ID")


if __name__ == "__main__":
    unittest.main()
