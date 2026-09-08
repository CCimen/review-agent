import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { APIError, read } from "./api";
import type { AuditEvent, AuditPage } from "./api";
import { useScope, ScopedLink as Link } from "./scope";
import { Empty, Freshness, time } from "./ui";

const filterFields = [
  "search",
  "action",
  "outcome",
  "actor_id",
  "since",
  "until",
] as const;
const actions: readonly AuditEvent["action"][] = [
  "team_created",
  "team_updated",
  "member_added",
  "member_updated",
  "member_removed",
  "repository_assigned",
  "repository_transferred",
  "repository_removed",
  "repository_requested",
  "request_approved",
  "request_rejected",
  "request_withdrawn",
  "account_created",
  "account_updated",
  "password_changed",
  "signed_in",
  "signed_out",
  "finding_decided",
  "feedback_triaged",
  "run_action",
  "settings_updated",
  "access_updated",
  "provider_login",
  "provider_login_cancelled",
];

function localTime(value: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
        .toISOString()
        .slice(0, 16);
}

function AuditExport({ filters }: { filters: string }) {
  const [format, setFormat] = useState("json");
  const request = useRef<AbortController | null>(null);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const exportPage = useMutation({
    mutationFn: async (before: string | null) => {
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      const params = new URLSearchParams(filters);
      params.set("format", format);
      if (before) params.set("before_id", before);
      const response = await fetch(`/api/audit/export?${params}`, {
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (!response.ok)
        throw new APIError(
          response.status,
          "The audit export could not be prepared. Check the filters and try again.",
        );
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download =
        /filename="([^"]+)"/.exec(
          response.headers.get("content-disposition") ?? "",
        )?.[1] ??
        `review-agent-audit.${format === "otlp" ? "otlp.json" : format}`;
      document.body.append(link);
      link.click();
      link.remove();
      // Let the browser start reading the blob before releasing its object URL.
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setCount(Number(response.headers.get("x-audit-count")));
      setNextBefore(response.headers.get("x-audit-next-before-id"));
    },
  });
  return (
    <div className="audit-export">
      <div className="toolbar">
        <label className="field">
          Export format
          <select
            value={format}
            disabled={exportPage.isPending}
            onChange={(event) => {
              setFormat(event.target.value);
              setCount(null);
              setNextBefore(null);
              exportPage.reset();
            }}
          >
            <option value="json">JSON</option>
            <option value="csv">CSV</option>
            <option value="jsonl">JSON Lines</option>
            <option value="otlp">OpenTelemetry (OTLP JSON)</option>
          </select>
        </label>
        <button
          className="secondary"
          disabled={exportPage.isPending}
          onClick={() => exportPage.mutate(null)}
        >
          {exportPage.isPending
            ? "Preparing export…"
            : "Export matching events"}
        </button>
        {nextBefore ? (
          <button
            className="secondary"
            disabled={exportPage.isPending}
            onClick={() => exportPage.mutate(nextBefore)}
          >
            Export next 1,000
          </button>
        ) : null}
      </div>
      <p className="field-hint" role="status">
        {count === null
          ? "Each file contains up to 1,000 matching events, newest first."
          : `${count.toLocaleString()} events downloaded.${nextBefore ? " More events are available in the next file." : " No older matching events remain."}`}
      </p>
      {exportPage.isError ? (
        <p className="notice error" role="alert">
          {exportPage.error.message}
        </p>
      ) : null}
    </div>
  );
}

export function AuditLog({ teamId }: { teamId?: number }) {
  const scope = useScope();
  const [location, setLocation] = useSearchParams();
  const before = location.get("before_id");
  const filters = new URLSearchParams();
  for (const field of filterFields) {
    const value = location.get(field);
    if (value) filters.set(field, value);
  }
  if (teamId) filters.set("team_id", String(teamId));
  const filterKey = filters.toString();
  const params = new URLSearchParams(filters);
  if (before) params.set("before_id", before);
  const setPage = (value: number | null) => {
    const next = new URLSearchParams(location);
    if (value) next.set("before_id", String(value));
    else next.delete("before_id");
    setLocation(next);
  };
  const query = useQuery({
    queryKey: ["audit", teamId, params.toString(), "scoped", scope.key],
    queryFn: ({ signal }) => read<AuditPage>(`/api/audit?${params}`, signal),
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
              Platform administration · All teams.{" "}
              {scope.current.role === "owner"
                ? "Account, team, and platform changes visible to owners."
                : "Team, repository, and member changes. Privileged account and sensitive platform changes are visible to owners."}
            </p>
          </div>
        </div>
      ) : (
        <h2>Team audit log</h2>
      )}
      <form
        className="audit-filters"
        key={filterKey}
        onSubmit={(event) => {
          event.preventDefault();
          const values = new FormData(event.currentTarget);
          const next = new URLSearchParams(location);
          next.delete("before_id");
          for (const field of filterFields) {
            const value = String(values.get(field) ?? "").trim();
            if (value)
              next.set(
                field,
                field === "since" || field === "until"
                  ? new Date(value).toISOString()
                  : value,
              );
            else next.delete(field);
          }
          setLocation(next);
        }}
      >
        <div className="toolbar">
          <label className="field grow">
            Search audit events
            <input
              name="search"
              type="search"
              maxLength={200}
              defaultValue={location.get("search") ?? ""}
              placeholder="Words in an actor, subject, reason, or change"
            />
          </label>
          <button className="secondary">Apply filters</button>
          {filterFields.some((field) => location.has(field)) ? (
            <button
              type="button"
              className="text-button"
              onClick={() => {
                const next = new URLSearchParams(location);
                for (const field of [...filterFields, "before_id"])
                  next.delete(field);
                setLocation(next);
              }}
            >
              Clear filters
            </button>
          ) : null}
        </div>
        <details
          className="audit-filter-details"
          open={
            filterFields.slice(1).some((field) => location.has(field)) ||
            undefined
          }
        >
          <summary>Filter by action, actor, outcome, or time</summary>
          <div className="form-fields">
            <label className="field">
              Action
              <select name="action" defaultValue={location.get("action") ?? ""}>
                <option value="">All actions</option>
                {actions.map((action) => (
                  <option key={action} value={action}>
                    {action.replaceAll("_", " ")}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              Outcome
              <select
                name="outcome"
                defaultValue={location.get("outcome") ?? ""}
              >
                <option value="">All outcomes</option>
                <option value="started">Started</option>
                <option value="succeeded">Succeeded</option>
                <option value="failed">Failed</option>
              </select>
            </label>
            <label className="field">
              Actor account ID
              <input
                name="actor_id"
                defaultValue={location.get("actor_id") ?? ""}
                placeholder="All actors"
                maxLength={36}
                pattern="[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
              />
            </label>
            <label className="field">
              From · local time
              <input
                name="since"
                type="datetime-local"
                defaultValue={localTime(location.get("since"))}
              />
            </label>
            <label className="field">
              Until · local time
              <input
                name="until"
                type="datetime-local"
                defaultValue={localTime(location.get("until"))}
              />
            </label>
          </div>
          <p className="field-hint">
            Search matches all supplied words. The start is included; the end is
            excluded.
          </p>
        </details>
      </form>
      <AuditExport key={filterKey} filters={filterKey} />
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
          Changes appear here after they are recorded. Try clearing the filters
          or returning to the latest events.
        </Empty>
      ) : null}
      {before || query.data?.next_before_id ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={!before}
            onClick={() => setPage(null)}
          >
            Latest events
          </button>
          <button
            className="secondary"
            disabled={!query.data?.next_before_id}
            onClick={() => setPage(query.data?.next_before_id ?? null)}
          >
            Older events
          </button>
        </div>
      ) : null}
    </>
  );
}
