"""Small admission implementation for the controlled review pilot."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Job:
    id: int
    tenant: str
    running: bool
    expires_at: datetime


def claim_jobs(
    jobs: list[Job],
    *,
    now: datetime,
    global_limit: int,
    tenant_limit: int | None = None,
) -> list[Job]:
    """Select live pending jobs within global and per-tenant concurrency limits.

    When no tenant override is supplied, each tenant may run at most two jobs.
    A pending job whose lease has expired must never be dispatched.
    """
    running = [job for job in jobs if job.running and job.expires_at > now]
    available = max(global_limit - len(running), 0)
    by_tenant: dict[str, int] = {}
    for job in running:
        by_tenant[job.tenant] = by_tenant.get(job.tenant, 0) + 1

    claimed: list[Job] = []
    for job in jobs:
        if job.running or len(claimed) >= available:
            continue
        count = by_tenant.get(job.tenant, 0)
        if tenant_limit is not None and count >= tenant_limit:
            continue
        claimed.append(job)
        by_tenant[job.tenant] = count + 1
    return claimed
