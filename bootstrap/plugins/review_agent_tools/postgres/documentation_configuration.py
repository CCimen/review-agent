"""Last operator configuration inspection; never an execution-policy cache."""

from datetime import datetime

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from ..documentation_configuration import DocumentationConfiguration


_ADAPTER = TypeAdapter(DocumentationConfiguration)


def latest(connection: psycopg.Connection[TupleRow], repository_id: int) -> DocumentationConfiguration | None:
    row = connection.execute(
        "SELECT snapshot FROM review_agent.repository_documentation_configuration WHERE repository_id = %s",
        (repository_id,),
    ).fetchone()
    return _ADAPTER.validate_python(row[0]) if row is not None else None


def save(
    connection: psycopg.Connection[TupleRow], *, repository_id: int,
    snapshot: DocumentationConfiguration, refresh_started_at: datetime,
) -> DocumentationConfiguration:
    # A slower earlier read must not overwrite a later refresh.
    connection.execute(
        """INSERT INTO review_agent.repository_documentation_configuration
               (repository_id, snapshot, refresh_started_at) VALUES (%s, %s, %s)
           ON CONFLICT (repository_id) DO UPDATE
             SET snapshot = EXCLUDED.snapshot, refresh_started_at = EXCLUDED.refresh_started_at
           WHERE repository_documentation_configuration.refresh_started_at <= EXCLUDED.refresh_started_at""",
        (repository_id, Jsonb(_ADAPTER.dump_python(snapshot, mode="json")), refresh_started_at),
    )
    current = latest(connection, repository_id)
    assert current is not None
    return current
