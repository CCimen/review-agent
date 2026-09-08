import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { read } from "./api";
import type { AuditPage } from "./api";
import { useScope, ScopedLink as Link } from "./scope";
import { Empty, Freshness, time } from "./ui";

export function AuditLog({ teamId }: { teamId?: number }) {
  const scope = useScope();
  const [before, setBefore] = useState<number | null>(null);
  const [actor, setActor] = useState("");
  const [actorDraft, setActorDraft] = useState("");
  const params = new URLSearchParams();
  if (before) params.set("before_id", String(before));
  if (actor && !teamId) params.set("actor_id", actor);
  const query = useQuery({
    queryKey: ["audit", teamId, before, actor, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<AuditPage>(
        `${teamId ? `/api/teams/${teamId}/events` : "/api/audit"}?${params}`,
        signal,
      ),
  });
  useEffect(() => {
    if (!teamId) document.title = "Review Agent · Audit log";
  }, [teamId]);
  return (
    <>
      {!teamId ? (
        <div className="page-heading">
          <div>
            <h1>Audit log</h1>
            <p>
              Platform administration ·{" "}
              {scope.current.role === "owner"
                ? "Account, team, and platform changes visible to owners."
                : "Team, repository, and member changes. Privileged account and sensitive platform changes are visible to owners."}
            </p>
          </div>
        </div>
      ) : (
        <h2>Team audit log</h2>
      )}
      {!teamId ? (
        <form
          className="toolbar search-form"
          onSubmit={(event) => {
            event.preventDefault();
            setActor(actorDraft.trim());
            setBefore(null);
          }}
        >
          <label className="field grow">
            Actor account ID
            <input
              value={actorDraft}
              placeholder="All actors"
              maxLength={36}
              pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
              onChange={(event) => setActorDraft(event.target.value)}
            />
          </label>
          <button className="secondary">Filter</button>
          {actor ? (
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setActor("");
                setActorDraft("");
                setBefore(null);
              }}
            >
              Clear filter
            </button>
          ) : null}
        </form>
      ) : null}
      <Freshness query={query} />
      {query.data?.items.length ? (
        <div
          className="panel table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Audit events"
        >
          <table className="audit-table">
            <thead>
              <tr>
                <th>When / action</th>
                <th>Actor</th>
                <th>Subject / reason</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((event) => (
                <tr key={event.id}>
                  <th scope="row">
                    <span>{event.action.replaceAll("_", " ")}</span>
                    <span className="subtext">{time(event.recorded_at)}</span>
                    {event.outcome !== "succeeded" ? (
                      <span className="status">
                        {event.outcome === "started"
                          ? "Started · outcome not yet recorded"
                          : "Failed"}
                      </span>
                    ) : null}
                  </th>
                  <td>
                    {event.actor_email ?? "System"}
                    <span className="subtext">{event.actor_role}</span>
                    <span className="subtext mono">{event.actor_id}</span>
                  </td>
                  <td>
                    <span className="mono">{event.subject}</span>
                    <p>{event.reason}</p>
                    {event.team_id && !teamId ? (
                      <Link
                        to={`/teams/${event.team_id}?team_id=${event.team_id}&tab=audit`}
                      >
                        Team #{event.team_id}
                      </Link>
                    ) : null}
                  </td>
                  <td>
                    <details>
                      <summary>View change</summary>
                      <dl className="audit-details">
                        {Object.entries(event.details).map(([key, value]) => (
                          <div key={key}>
                            <dt>{key.replaceAll("_", " ")}</dt>
                            <dd>{value == null ? "—" : String(value)}</dd>
                          </div>
                        ))}
                      </dl>
                      {event.operation_id ? (
                        <p className="mono">Operation {event.operation_id}</p>
                      ) : null}
                      <span className="subtext">
                        Event #{event.id}
                        {event.owner_only ? " · Owners only" : ""}
                      </span>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : query.data ? (
        <Empty title="No audit events in this view">
          Changes appear here after they are recorded. Try clearing the actor
          filter or returning to the latest events.
        </Empty>
      ) : null}
      {before || query.data?.next_before_id ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={!before}
            onClick={() => setBefore(null)}
          >
            Latest events
          </button>
          <button
            className="secondary"
            disabled={!query.data?.next_before_id}
            onClick={() => setBefore(query.data?.next_before_id ?? null)}
          >
            Older events
          </button>
        </div>
      ) : null}
    </>
  );
}
