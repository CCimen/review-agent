"""PostgreSQL relations for the finding occurrences and decisions visible in a published review."""

OCCURRENCES = """(
    SELECT occurrence.*, item.id AS publication_finding_id, item.local_reference
    FROM review_agent.finding_occurrences AS occurrence
    JOIN review_agent.publications AS publication
      ON publication.review_run_id = occurrence.review_run_id
    JOIN review_agent.publication_findings AS item
      ON item.publication_id = publication.id AND item.finding_id = occurrence.finding_id
     AND item.source_finding_occurrence_id = occurrence.id
    WHERE publication.status = 'posted' AND item.outcome = 'current'
)"""

DECISIONS = f"""(
    SELECT decision.* FROM review_agent.finding_decisions AS decision
    JOIN {OCCURRENCES} AS occurrence ON occurrence.id = decision.finding_occurrence_id
)"""
