#!/usr/bin/env python3
"""Human administration for the Review Agent PostgreSQL store."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
import sys
from typing import NoReturn, cast

from review_agent_coach_run import build_coach_run_artifacts
from review_agent_private_io import write_private_file


def _plugin_parent() -> Path:
    candidates = (
        Path("/opt/review-agent-bootstrap/plugins"),
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "plugins",
        Path(__file__).resolve().parents[1] / "bootstrap" / "plugins",
    )
    for candidate in candidates:
        if (candidate / "review_agent_tools" / "operator_application.py").exists():
            sys.path.insert(0, str(candidate))
            return candidate
    raise SystemExit("Could not locate the review_agent_tools package")


_plugin_parent()

from review_agent_tools import operator_application  # noqa: E402
from review_agent_tools.domain.coaching import CoachCandidateInput  # noqa: E402
from review_agent_tools.github.publication import GitHubIssueCommentGateway  # noqa: E402
from review_agent_tools.postgres.runtime import (  # noqa: E402
    PostgreSQLRuntime,
    PostgreSQLRuntimeRole,
)
from review_agent_tools.review_publication_application import (  # noqa: E402
    publish_postgres_run_failure_status,
)
from review_agent_tools.settings import ReviewAgentSettings  # noqa: E402


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json(value: object, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=True,
    )


def _runtime() -> PostgreSQLRuntime:
    runtime = PostgreSQLRuntime(
        ReviewAgentSettings.from_environment().postgres_database_url,
        role=PostgreSQLRuntimeRole.OPERATOR,
    )
    runtime.open()
    return runtime


def _write_or_print(content: str, output: str | None) -> None:
    if output:
        destination = Path(output)
        write_private_file(destination, content)
        print(destination)
    else:
        print(content, end="" if content.endswith("\n") else "\n")


def _complete_export(path: str) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit("export must contain a JSON object")
    state = cast(dict[str, object], value)
    if state.get("complete") is not True:
        raise SystemExit(
            "export is incomplete; rerun with a larger --row-limit before learning "
            f"or coaching (truncated_tables={state.get('truncated_tables', [])})"
        )
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List recent findings.")
    listing.add_argument("--repo")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--open-only", action="store_true")
    show = commands.add_parser("show", help="Show one finding and its decisions.")
    show.add_argument("fingerprint")
    show.add_argument("--repo", required=True)
    decide = commands.add_parser("decide", help="Append a human decision.")
    decide.add_argument("fingerprint")
    decide.add_argument("decision")
    decide.add_argument("--repo", required=True)
    decide.add_argument("--reason", required=True)
    decide.add_argument("--actor", required=True)
    decide.add_argument("--expires-days", type=int)
    decide.add_argument("--adr-id", default="")
    decide.add_argument("--occurrence-id", type=int)
    decide.add_argument("--pr", type=int)
    decide.add_argument("--local-reference", default="")
    decide.add_argument("--latest", action="store_true")
    export = commands.add_parser("export", help="Export one repository as JSON.")
    export.add_argument("--repo", required=True)
    export.add_argument("--row-limit", type=int, required=True)
    export.add_argument("--output")
    stats = commands.add_parser("stats", help="Summarize finding state.")
    stats.add_argument("--repo")
    stats.add_argument("--expiring-within-days", type=int, default=30)
    runs = commands.add_parser("runs", help="Inspect or recover review runs.")
    runs.add_argument("--repo")
    runs.add_argument("--pr", type=int)
    runs.add_argument("--limit", type=int, default=50)
    runs.add_argument("--failed", action="store_true")
    runs.add_argument("--stats", action="store_true")
    runs.add_argument("--days", type=int, default=30)
    runs.add_argument("--stale-after-minutes", type=int, default=30)
    runs.add_argument("--mark-stalled", action="store_true")
    runs.add_argument("--publish-failure-status", action="store_true")
    publications = commands.add_parser("publications", help="List publications.")
    publications.add_argument("--repo")
    publications.add_argument("--pr", type=int)
    publications.add_argument("--limit", type=int, default=50)
    coverage = commands.add_parser("coverage", help="Show run coverage.")
    coverage.add_argument("--run-id", type=int, required=True)
    verification = commands.add_parser(
        "verification-export", help="Write a private verification bundle."
    )
    verification.add_argument("--run-id", type=int, required=True)
    verification.add_argument("--output", required=True)
    learning = commands.add_parser(
        "learning-report", help="Build a report from a complete export."
    )
    learning.add_argument("--export", required=True)
    learning.add_argument("--repo")
    learning.add_argument("--output")
    replay = commands.add_parser("validate-replay", help="Validate replay fixtures.")
    replay.add_argument("path", type=Path)
    coach_export = commands.add_parser("coach-export", help="Build coach events.")
    coach_export.add_argument("--export", required=True)
    coach_export.add_argument("--repo")
    coach_export.add_argument("--after-decision-id", type=int, default=0)
    coach_export.add_argument("--after-feedback-id", type=int, default=0)
    coach_export.add_argument("--include-incomplete", action="store_true")
    coach_export.add_argument("--output", required=True)
    propose = commands.add_parser("coach-propose", help="Build coach proposals.")
    propose.add_argument("--events", required=True)
    propose.add_argument("--output-dir", required=True)
    propose.add_argument("--max-candidates", type=int, default=3)
    propose.add_argument("--min-independent-episodes", type=int, default=2)
    verify = commands.add_parser(
        "coach-verify-proposal", help="Verify a coach proposal."
    )
    verify.add_argument("--proposal", required=True)
    coach_run = commands.add_parser("coach-run", help="Run and record the coach.")
    coach_run.add_argument("--export")
    coach_run.add_argument("--repo", required=True)
    coach_run.add_argument("--row-limit", type=int, default=10_000)
    coach_run.add_argument("--output-dir", required=True)
    coach_run.add_argument("--after-decision-id", type=int, default=0)
    coach_run.add_argument("--after-feedback-id", type=int, default=0)
    coach_run.add_argument("--include-incomplete", action="store_true")
    coach_run.add_argument("--max-candidates", type=int, default=3)
    coach_run.add_argument("--min-independent-episodes", type=int, default=2)
    return parser


def _offline_command(args: argparse.Namespace) -> int | None:
    if args.command == "learning-report":
        import review_agent_learning as learning
        report = learning.build_learning_report(
            _complete_export(args.export), repository=args.repo
        )
        _write_or_print(learning.render_markdown(report), args.output)
        return 0
    if args.command == "validate-replay":
        import review_agent_replay as replay
        results = replay.validate_replay_path(args.path)
        for result in results:
            print(f"Replay OK: {result.fixture_id} ({result.path})")
        print(f"Validated {len(results)} replay fixture(s).")
        return 0
    if args.command == "coach-export":
        import review_agent_coach as coach
        payload = coach.build_coach_export(
            _complete_export(args.export),
            repository=args.repo,
            after_decision_id=args.after_decision_id,
            after_feedback_id=args.after_feedback_id,
            include_incomplete=args.include_incomplete,
        )
        _write_or_print(coach.dumps_coach_export(payload), args.output)
        return 0
    if args.command == "coach-propose":
        import review_agent_coach_proposals as proposals
        bundle = proposals.build_proposal(
            proposals.load_coach_export(Path(args.events)),
            max_candidates=args.max_candidates,
            min_independent_episodes=args.min_independent_episodes,
        )
        output_dir = Path(args.output_dir)
        write_private_file(
            output_dir / "proposal.json", proposals.dumps_proposal_bundle(bundle)
        )
        write_private_file(output_dir / "SUMMARY.md", proposals.render_markdown(bundle))
        print(output_dir)
        return 0
    if args.command == "coach-verify-proposal":
        import review_agent_coach_proposals as proposals
        bundle = proposals.load_proposal_bundle(Path(args.proposal))
        print(_json(proposals.verify_proposal_bundle(bundle).to_json_obj(), pretty=True))
        return 0
    return None


def _fatal(exc: Exception) -> NoReturn:
    raise SystemExit(str(exc)) from exc


def _run_live(args: argparse.Namespace, runtime: PostgreSQLRuntime) -> int:
    result: object
    if args.command == "list":
        result = operator_application.list_findings(
            runtime, repository=args.repo, limit=args.limit,
            include_suppressed=not args.open_only,
        )
    elif args.command == "show":
        result = operator_application.show_finding(
            runtime, repository=args.repo, fingerprint=args.fingerprint
        )
    elif args.command == "decide":
        result = operator_application.decide_finding(
            runtime,
            operator_application.OperatorDecisionRequest(
                repository=args.repo, fingerprint=args.fingerprint,
                decision=args.decision, reason=args.reason, actor=args.actor,
                occurrence_id=args.occurrence_id, pr_number=args.pr,
                local_reference=args.local_reference, latest=args.latest,
                expires_days=args.expires_days, adr_id=args.adr_id,
            ),
        )
    elif args.command == "export":
        export = operator_application.export_repository(
            runtime, repository=args.repo, row_limit=args.row_limit
        )
        _write_or_print(_json(export.to_json_obj(), pretty=True) + "\n", args.output)
        return 0
    elif args.command == "stats":
        result = operator_application.finding_stats(
            runtime, repository=args.repo,
            expiring_within_days=args.expiring_within_days,
        )
    elif args.command == "runs":
        if args.publish_failure_status:
            queue = operator_application.prepare_failure_status_queue(
                runtime, repository=args.repo, pr_number=args.pr,
                older_than_minutes=args.stale_after_minutes, limit=args.limit,
            )
            configured = ReviewAgentSettings.from_environment()
            github = GitHubIssueCommentGateway(
                configured.github_publish_token,
                read_token=configured.github_read_token,
            )
            failures: list[dict[str, object]] = []
            posted = 0
            for target in queue.targets:
                try:
                    publish_postgres_run_failure_status(
                        runtime, run_id=int(target.run_id), github=github
                    )
                    posted += 1
                except Exception as exc:
                    failures.append({"run_id": int(target.run_id), "error": str(exc)})
            print(_json({"marked_failed": queue.marked.failed_count,
                         "status_posted": posted, "status_failed": failures}, pretty=True))
            return 1 if failures else 0
        if args.mark_stalled:
            result = operator_application.mark_stalled_runs(
                runtime, repository=args.repo, pr_number=args.pr,
                older_than_minutes=args.stale_after_minutes,
            )
        elif args.stats:
            result = operator_application.run_stats(
                runtime, repository=args.repo, days=args.days,
                stale_after_minutes=args.stale_after_minutes,
            )
        else:
            result = operator_application.list_runs(
                runtime, repository=args.repo, limit=args.limit,
                failed_only=args.failed,
            )
    elif args.command == "publications":
        result = operator_application.list_publications(
            runtime, repository=args.repo, pr_number=args.pr, limit=args.limit
        )
    elif args.command == "coverage":
        result = operator_application.coverage(runtime, run_id=args.run_id)
    elif args.command == "verification-export":
        import review_agent_verification as verification
        source = operator_application.verification_export_source(
            runtime, run_id=args.run_id
        )
        payload = verification.build_verification_export(source, coverage=None)
        _write_or_print(verification.dumps_verification_export(payload), args.output)
        return 0
    elif args.command == "coach-run":
        if args.export:
            state = _complete_export(args.export)
        else:
            export = operator_application.export_repository(
                runtime, repository=args.repo, row_limit=args.row_limit
            )
            state = cast(dict[str, object], export.to_json_obj())
            if state.get("complete") is not True:
                raise SystemExit("live coach export is incomplete; increase --row-limit")
        artifacts = build_coach_run_artifacts(
            state=state, output_dir=Path(args.output_dir), repository=args.repo,
            after_decision_id=args.after_decision_id,
            after_feedback_id=args.after_feedback_id,
            include_incomplete=args.include_incomplete,
            max_candidates=args.max_candidates,
            min_independent_episodes=args.min_independent_episodes,
        )
        candidates = tuple(
            CoachCandidateInput(
                candidate_key=item.candidate_key, target_owner=item.target_owner,
                suggested_route=item.suggested_route, event_type=item.event_type,
                independent_episode_count=item.independent_episode_count,
                evidence_event_ids=item.evidence_event_ids,
                evidence_events_total=item.evidence_events_total,
            ) for item in artifacts.bundle.candidates
        )
        run = operator_application.record_coach_run(
            runtime, repository=artifacts.bundle.repository_untrusted,
            source_event_set_id=artifacts.bundle.source_event_set_id,
            source_snapshot_id=artifacts.bundle.source_snapshot_id,
            proposal_set_id=artifacts.bundle.proposal_set_id,
            events_considered=artifacts.bundle.events_considered,
            artifact_dir=str(artifacts.paths.output_dir), candidates=candidates,
        )
        result = {"run": run, "artifacts": artifacts.paths.to_json_obj()}
    else:
        raise SystemExit(f"unsupported command: {args.command}")
    print(_json(result, pretty=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    offline = _offline_command(args)
    if offline is not None:
        return offline
    try:
        runtime = _runtime()
    except Exception as exc:
        _fatal(exc)
    try:
        return _run_live(args, runtime)
    except Exception as exc:
        _fatal(exc)
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
