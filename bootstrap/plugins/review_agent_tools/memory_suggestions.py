"""Validated, head-scoped atomic suggestions for published PR findings."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
import re
import sqlite3
from typing import Literal, TypedDict, cast

try:
    from . import suggestion_validation as _suggestion_validation
    from .memory_validation import (
        ReviewMemoryError,
        isoformat,
        parse_time,
        utc_now,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    import suggestion_validation as _suggestion_validation  # type: ignore[no-redef]
    from memory_validation import (  # type: ignore[no-redef]
        ReviewMemoryError,
        isoformat,
        parse_time,
        utc_now,
    )


MAX_ATOMIC_SUGGESTIONS_PER_REVIEW = (
    _suggestion_validation.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW
)
MAX_SUGGESTION_RANGE_LINES = _suggestion_validation.MAX_SUGGESTION_RANGE_LINES
MAX_SUGGESTION_REPLACEMENT_LINES = (
    _suggestion_validation.MAX_SUGGESTION_REPLACEMENT_LINES
)
MAX_SUGGESTION_TEXT_CHARS = _suggestion_validation.MAX_SUGGESTION_TEXT_CHARS
SUGGESTION_MARKER_PREFIX = _suggestion_validation.SUGGESTION_MARKER_PREFIX
SuggestionValidation = _suggestion_validation.SuggestionValidation
ValidatedSuggestion = _suggestion_validation.ValidatedSuggestion
suggestion_eligibility_rejection = (
    _suggestion_validation.suggestion_eligibility_rejection
)
suggestion_key = _suggestion_validation.suggestion_key
validate_suggestion = _suggestion_validation.validate_suggestion


SUGGESTION_POSTING_LEASE_MINUTES = 30


class PublicationSuggestion(ValidatedSuggestion):
    observation_id: int
    review_run_id: int
    fingerprint: str
    local_reference: str


SuggestionDeliveryState = Literal[
    "none", "pending", "posting", "posted", "publish_failed", "stale"
]


class SuggestionDelivery(TypedDict):
    suggestion_delivery_status: SuggestionDeliveryState
    suggestion_review_id: int | None
    suggestion_posting_started_at: str | None
    suggestion_posted_at: str | None
    suggestion_failure_code: str


class SuggestionClaim(SuggestionDelivery):
    claimed: bool


def replace_observation_suggestion(
    connection: sqlite3.Connection,
    *,
    observation_id: int,
    suggestion: ValidatedSuggestion | None,
) -> None:
    """Replace one run observation's optional suggestion inside the caller transaction."""
    if isinstance(observation_id, bool) or int(observation_id) < 1:
        raise ReviewMemoryError("observation_id must be positive")
    observation = connection.execute(
        """
        SELECT id, review_run_id, repository, pr_number, head_sha, fingerprint,
               rule_id, category, path, symbol, anchor, title,
               evidence, impact, smallest_fix
        FROM finding_observations
        WHERE id = ?
        """,
        (int(observation_id),),
    ).fetchone()
    if observation is None:
        raise ReviewMemoryError("observation_id does not identify a finding observation")
    if suggestion is None:
        connection.execute(
            "DELETE FROM review_suggestions WHERE observation_id = ?",
            (int(observation_id),),
        )
        return
    review_run_id = observation["review_run_id"]
    if review_run_id is None:
        raise ReviewMemoryError("suggestions require a run-owned finding observation")
    if suggestion["path"] != str(observation["path"]):
        raise ReviewMemoryError("suggestion path does not match its finding observation")
    rejection = suggestion_eligibility_rejection(
        rule_id=str(observation["rule_id"]),
        category=str(observation["category"]),
        path=str(observation["path"]),
        symbol=str(observation["symbol"]),
        anchor=str(observation["anchor"]),
        title=str(observation["title"]),
        evidence=str(observation["evidence"]),
        impact=str(observation["impact"]),
        smallest_fix=str(observation["smallest_fix"]),
    )
    if rejection:
        raise ReviewMemoryError(rejection)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", suggestion["suggestion_key"]):
        raise ReviewMemoryError("suggestion_key is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", suggestion["expected_hash"]):
        raise ReviewMemoryError("suggestion expected_hash is invalid")
    expected_key = suggestion_key(
        str(observation["repository"]),
        int(observation["pr_number"]),
        str(observation["head_sha"]),
        str(observation["fingerprint"]),
    )
    if suggestion["suggestion_key"] != expected_key:
        raise ReviewMemoryError("suggestion_key does not match its finding observation")
    canonical = connection.execute(
        """
        SELECT path, start_line, end_line, expected_hash, replacement_text,
               suggestion_key
        FROM review_suggestions
        WHERE suggestion_key = ?
        ORDER BY id
        LIMIT 1
        """,
        (suggestion["suggestion_key"],),
    ).fetchone()
    stored = suggestion
    if canonical is not None:
        if str(canonical["path"]) != str(observation["path"]):
            raise ReviewMemoryError("canonical suggestion path is inconsistent")
        stored = {
            "path": str(canonical["path"]),
            "start_line": int(canonical["start_line"]),
            "end_line": int(canonical["end_line"]),
            "expected_hash": str(canonical["expected_hash"]),
            "replacement_text": str(canonical["replacement_text"]),
            "suggestion_key": str(canonical["suggestion_key"]),
        }
    connection.execute(
        "DELETE FROM review_suggestions WHERE observation_id = ?",
        (int(observation_id),),
    )
    connection.execute(
        """
        INSERT INTO review_suggestions (
            observation_id, review_run_id, repository, pr_number, head_sha,
            fingerprint, path, start_line, end_line, expected_hash,
            replacement_text, suggestion_key, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(observation_id),
            int(review_run_id),
            str(observation["repository"]),
            int(observation["pr_number"]),
            str(observation["head_sha"]),
            str(observation["fingerprint"]),
            stored["path"],
            stored["start_line"],
            stored["end_line"],
            stored["expected_hash"],
            stored["replacement_text"],
            stored["suggestion_key"],
            isoformat(),
        ),
    )


def suggestions_for_publication(
    connection: sqlite3.Connection, publication_id: int
) -> list[PublicationSuggestion]:
    if isinstance(publication_id, bool) or int(publication_id) < 1:
        raise ReviewMemoryError("publication_id must be positive")
    rows = connection.execute(
        """
        SELECT rs.observation_id, rs.review_run_id, rs.fingerprint, rs.path,
               rs.start_line, rs.end_line, rs.expected_hash,
               rs.replacement_text, rs.suggestion_key, pf.local_reference
        FROM publication_findings pf
        JOIN review_suggestions rs ON rs.observation_id = pf.observation_id
        WHERE pf.publication_id = ? AND pf.status = 'current'
        ORDER BY CAST(SUBSTR(pf.local_reference, 2) AS INTEGER), pf.id
        """,
        (int(publication_id),),
    ).fetchall()
    return [
        PublicationSuggestion(
            observation_id=int(row["observation_id"]),
            review_run_id=int(row["review_run_id"]),
            fingerprint=str(row["fingerprint"]),
            local_reference=str(row["local_reference"]),
            path=str(row["path"]),
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            expected_hash=str(row["expected_hash"]),
            replacement_text=str(row["replacement_text"]),
            suggestion_key=str(row["suggestion_key"]),
        )
        for row in rows
    ]


def canonical_suggestions(
    connection: sqlite3.Connection, keys: Iterable[str]
) -> dict[str, ValidatedSuggestion]:
    """Return the first validated patch for each finding/head identity."""
    normalized = sorted(
        {
            str(key)
            for key in keys
            if re.fullmatch(r"sha256:[0-9a-f]{64}", str(key))
        }
    )
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = connection.execute(
        f"""
        SELECT path, start_line, end_line, expected_hash, replacement_text,
               suggestion_key
        FROM review_suggestions
        WHERE suggestion_key IN ({placeholders})
        ORDER BY id
        """,
        normalized,
    ).fetchall()
    canonical: dict[str, ValidatedSuggestion] = {}
    for row in rows:
        key = str(row["suggestion_key"])
        canonical.setdefault(
            key,
            {
                "path": str(row["path"]),
                "start_line": int(row["start_line"]),
                "end_line": int(row["end_line"]),
                "expected_hash": str(row["expected_hash"]),
                "replacement_text": str(row["replacement_text"]),
                "suggestion_key": key,
            },
        )
    return canonical


def prepare_publication_suggestions(
    connection: sqlite3.Connection, publication_id: int
) -> int:
    suggestions = suggestions_for_publication(connection, publication_id)
    connection.execute(
        """
        UPDATE review_publications
        SET suggestion_delivery_status = ?, suggestion_failure_code = ''
        WHERE id = ?
        """,
        ("pending" if suggestions else "none", int(publication_id)),
    )
    return len(suggestions)


def suggestion_delivery_status(
    connection: sqlite3.Connection, publication_id: int
) -> SuggestionDelivery:
    row = connection.execute(
        """
        SELECT suggestion_delivery_status, suggestion_review_id,
               suggestion_posting_started_at, suggestion_posted_at,
               suggestion_failure_code
        FROM review_publications
        WHERE id = ?
        """,
        (int(publication_id),),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("publication_id was not found")
    state = str(row["suggestion_delivery_status"])
    if state not in {"none", "pending", "posting", "posted", "publish_failed", "stale"}:
        raise ReviewMemoryError("publication has an unknown suggestion delivery status")
    review_id = row["suggestion_review_id"]
    return {
        "suggestion_delivery_status": cast(SuggestionDeliveryState, state),
        "suggestion_review_id": int(review_id) if review_id is not None else None,
        "suggestion_posting_started_at": (
            str(row["suggestion_posting_started_at"])
            if row["suggestion_posting_started_at"] is not None
            else None
        ),
        "suggestion_posted_at": (
            str(row["suggestion_posted_at"])
            if row["suggestion_posted_at"] is not None
            else None
        ),
        "suggestion_failure_code": str(row["suggestion_failure_code"] or ""),
    }


def claim_suggestions_for_posting(
    connection: sqlite3.Connection,
    publication_id: int,
    *,
    now: datetime | None = None,
) -> SuggestionClaim:
    """Atomically claim one publication's independent suggestion delivery."""
    moment = now or utc_now()
    started_at = isoformat(moment)
    lease_cutoff = moment - timedelta(minutes=SUGGESTION_POSTING_LEASE_MINUTES)
    connection.execute("BEGIN IMMEDIATE")
    try:
        delivery = suggestion_delivery_status(connection, publication_id)
        state = delivery["suggestion_delivery_status"]
        if state == "posting":
            prior_started_at = delivery["suggestion_posting_started_at"]
            abandoned = prior_started_at is None
            if prior_started_at is not None:
                try:
                    parsed_started_at = parse_time(prior_started_at)
                    abandoned = (
                        parsed_started_at is None or parsed_started_at < lease_cutoff
                    )
                except ValueError:
                    abandoned = True
            if abandoned:
                connection.execute(
                    """
                    UPDATE review_publications
                    SET suggestion_delivery_status = 'publish_failed',
                        suggestion_failure_code = 'abandoned_suggestion_claim'
                    WHERE id = ? AND suggestion_delivery_status = 'posting'
                    """,
                    (int(publication_id),),
                )
                delivery = suggestion_delivery_status(connection, publication_id)
                state = delivery["suggestion_delivery_status"]
        if state not in {"pending", "publish_failed"}:
            connection.commit()
            return {**delivery, "claimed": False}
        cursor = connection.execute(
            """
            UPDATE review_publications
            SET suggestion_delivery_status = 'posting',
                suggestion_posting_started_at = ?,
                suggestion_failure_code = ''
            WHERE id = ?
              AND suggestion_delivery_status IN ('pending', 'publish_failed')
            """,
            (started_at, int(publication_id)),
        )
        if cursor.rowcount != 1:
            raise ReviewMemoryError("suggestion delivery was claimed by another publisher")
        claimed = suggestion_delivery_status(connection, publication_id)
        connection.commit()
        return {**claimed, "claimed": True}
    except Exception:
        connection.rollback()
        raise


def renew_suggestion_claim(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    claim_started_at: str,
    now: datetime | None = None,
) -> str:
    """Renew and fence one live suggestion-publication claim."""
    renewed_at = isoformat(now)
    with connection:
        cursor = connection.execute(
            """
            UPDATE review_publications
            SET suggestion_posting_started_at = ?
            WHERE id = ?
              AND suggestion_delivery_status = 'posting'
              AND suggestion_posting_started_at = ?
            """,
            (renewed_at, int(publication_id), claim_started_at),
        )
        if cursor.rowcount != 1:
            raise ReviewMemoryError("suggestion delivery claim was lost")
    return renewed_at


def mark_suggestions_posted(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_id: int,
    claim_started_at: str,
) -> None:
    if isinstance(review_id, bool) or int(review_id) < 1:
        raise ReviewMemoryError("review_id must be positive")
    with connection:
        cursor = connection.execute(
            """
            UPDATE review_publications
            SET suggestion_delivery_status = 'posted', suggestion_review_id = ?,
                suggestion_posting_started_at = NULL,
                suggestion_posted_at = ?, suggestion_failure_code = ''
            WHERE id = ?
              AND suggestion_delivery_status = 'posting'
              AND suggestion_posting_started_at = ?
            """,
            (
                int(review_id),
                isoformat(),
                int(publication_id),
                claim_started_at,
            ),
        )
        if cursor.rowcount != 1:
            raise ReviewMemoryError("suggestion delivery claim was lost")


def mark_suggestions_failed(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    failure_code: str,
    stale: bool = False,
    claim_started_at: str | None = None,
) -> None:
    code = str(failure_code or "suggestion_publish_failed").strip()[:160]
    with connection:
        if claim_started_at is not None:
            cursor = connection.execute(
                """
                UPDATE review_publications
                SET suggestion_delivery_status = ?, suggestion_failure_code = ?
                WHERE id = ?
                  AND suggestion_delivery_status = 'posting'
                  AND suggestion_posting_started_at = ?
                """,
                (
                    "stale" if stale else "publish_failed",
                    code,
                    int(publication_id),
                    claim_started_at,
                ),
            )
        else:
            allowed = (
                ("pending", "posted", "publish_failed", "stale")
                if stale
                else ("pending", "publish_failed")
            )
            placeholders = ",".join("?" for _ in allowed)
            cursor = connection.execute(
                f"""
                UPDATE review_publications
                SET suggestion_delivery_status = ?, suggestion_failure_code = ?
                WHERE id = ?
                  AND suggestion_delivery_status IN ({placeholders})
                """,
                (
                    "stale" if stale else "publish_failed",
                    code,
                    int(publication_id),
                    *allowed,
                ),
            )
        if cursor.rowcount != 1:
            raise ReviewMemoryError("suggestion delivery state could not be changed")


def suggestion_marker(key: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(key or "")):
        raise ReviewMemoryError("suggestion_key is invalid")
    return f"<!-- {SUGGESTION_MARKER_PREFIX}{key} -->"


def extract_suggestion_key(body: str) -> str | None:
    match = re.search(
        rf"(?:^|\n)<!--\s*{re.escape(SUGGESTION_MARKER_PREFIX)}"
        rf"(sha256:[0-9a-f]{{64}})\s*-->\s*\Z",
        str(body or ""),
    )
    return match.group(1) if match else None
