import {
  ScopedLink as Link,
  ScopedNavLink as NavLink,
  useScope,
} from "./scope";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { read } from "./api";
import type { HistoryItem, HistoryPage, Overview } from "./api";
import {
  Empty,
  Freshness,
  Period,
  Stat,
  failureSentence,
  number,
  since,
  time,
  useFilters,
} from "./ui";

const states: Record<HistoryItem["state"], string> = {
  queued: "Queued",
  running: "Reviewing",
  publishing: "Publishing",
  published: "Published",
  failed: "Failed",
  superseded: "Superseded",
};
const phases = [
  "accepted",
  "fetching_pr",
  "collecting_diff",
  "reviewing",
  "rendering",
  "publishing",
  "posted",
];
const phaseLabels: Record<string, string> = {
  accepted: "Waiting for a worker",
  fetching_pr: "Fetching pull request",
  collecting_diff: "Reading changes",
  reviewing: "Reviewing code",
  rendering: "Preparing the result",
  publishing: "Publishing to GitHub",
  posted: "Review published",
  failed: "Review stopped",
};

export function ActivityTabs() {
  return (
    <nav className="page-tabs" aria-label="Activity views">
      <NavLink to="/" end>
        Requests
      </NavLink>
      <NavLink to="/history">Pull requests</NavLink>
      <NavLink to="/overview">Statistics</NavLink>
    </nav>
  );
}

export function Phase({ item }: { item: HistoryItem }) {
  const index = phases.indexOf(item.phase);
  return (
    <span className="phase-cell">
      {item.state === "running" || item.state === "publishing" ? (
        <span className="phase-track" aria-hidden="true">
          {phases.map((phase, i) => (
            <span
              key={phase}
              className={
                i < index
                  ? "done"
                  : i === index
                    ? item.state === "failed"
                      ? "failed"
                      : "current"
                    : ""
              }
            />
          ))}
        </span>
      ) : null}
      <span>{phaseLabels[item.phase] ?? item.phase.replaceAll("_", " ")}</span>
    </span>
  );
}

export function ActivityPage() {
  const scope = useScope();
  const { params, days, update } = useFilters();
  const status = params.get("status") ?? "all";
  const [repository, setRepository] = useState(params.get("repository") ?? "");
  const [pr, setPr] = useState(params.get("pr_number") ?? "");
  const queryParams = new URLSearchParams({
    days: String(days),
    status,
    limit: "50",
  });
  for (const key of ["repository", "pr_number", "before_id"]) {
    const value = params.get(key);
    if (value) queryParams.set(key, value);
  }
  useEffect(() => {
    document.title = "Review Agent · Activity";
  }, []);
  useEffect(() => {
    setRepository(params.get("repository") ?? "");
    setPr(params.get("pr_number") ?? "");
  }, [params]);
  const query = useQuery({
    queryKey: ["activity", queryParams.toString(), "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<HistoryPage>(scope.path(`/api/history?${queryParams}`), signal),
  });
  const overview = useQuery({
    queryKey: ["overview", days, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<Overview>(scope.path(`/api/overview?days=${days}`), signal),
  });
  const data = overview.data;
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Activity</h1>
          <p>Follow review requests and open their results.</p>
        </div>
        <Period days={days} change={(value) => update({ days: value })} />
      </div>
      <ActivityTabs />
      <div className="activity-metrics">
        <Stat
          label="Active requests"
          value={data?.active_requests ?? null}
          hint="Across all reporting periods"
        />
        <Stat
          label="Published"
          value={data?.window.published_reviews ?? null}
          hint={`Last ${days} days`}
        />
        <Stat
          label="Failed"
          value={data?.window.failed_requests ?? null}
          hint="Includes earlier failures followed by a successful review"
          attention={(data?.window.failed_requests ?? 0) > 0}
        />
        {data?.live_review_workers != null ? (
          <Stat
            label="Review workers online"
            value={data?.live_review_workers ?? null}
            hint={
              data
                ? `${data.review_capacity} slots reported by online workers`
                : undefined
            }
          />
        ) : null}
      </div>
      <Freshness query={overview} quiet />
      <div className="toolbar activity-toolbar">
        <div
          className="segmented"
          role="group"
          aria-label="Filter request state"
        >
          {(
            [
              ["all", "All requests"],
              ["active", "Active"],
              ["published", "Published"],
              ["failed", "Failed"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              aria-pressed={status === value}
              onClick={() => update({ status: value })}
            >
              {label}
            </button>
          ))}
        </div>
        <form
          className="search-form"
          onSubmit={(event) => {
            event.preventDefault();
            update({ repository: repository.trim(), pr_number: pr });
          }}
        >
          <label className="field" htmlFor="activity-repo">
            Repository
            <input
              id="activity-repo"
              type="search"
              placeholder="owner/repository (optional)"
              maxLength={200}
              value={repository}
              onChange={(event) => setRepository(event.target.value)}
            />
          </label>
          <label className="field" htmlFor="activity-pr">
            PR number
            <input
              id="activity-pr"
              type="number"
              min="1"
              placeholder="Any"
              value={pr}
              onChange={(event) => setPr(event.target.value)}
            />
          </label>
          <button className="secondary" type="submit">
            Filter
          </button>
        </form>
        {params.has("repository") ||
        params.has("pr_number") ||
        params.has("before_id") ? (
          <button
            className="text-button"
            onClick={() =>
              update({ repository: "", pr_number: "", before_id: "" })
            }
          >
            Clear filters
          </button>
        ) : null}
      </div>
      <Freshness query={query} />
      {query.data &&
        (query.data.items.length ? (
          <section className="panel">
            <div className="panel-heading">
              <h2>Review requests</h2>
              <span>
                {number.format(query.data.total)} matching ·{" "}
                {query.data.items.length} shown
              </span>
            </div>
            <div
              className="table-scroll"
              role="region"
              tabIndex={0}
              aria-label="Review requests"
            >
              <table className="activity-table">
                <thead>
                  <tr>
                    <th scope="col">Pull request</th>
                    <th scope="col">State</th>
                    <th scope="col">Progress</th>
                    <th scope="col">Commit</th>
                    <th scope="col" className="numeric">
                      Findings
                    </th>
                    <th scope="col" className="numeric">
                      Attempts used
                    </th>
                    <th scope="col">Last activity</th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.items.map((item) => (
                    <tr key={item.id}>
                      <th scope="row">
                        <Link
                          className="run-name"
                          to={`/history/${item.id}?${params}`}
                        >
                          <span>{item.repository}</span>{" "}
                          <span className="pr-number">#{item.pr_number}</span>
                        </Link>
                        <span className="subtext">
                          Request #{item.id} · {time(item.started_at)}
                        </span>
                        {item.failure_code ? (
                          <span className="subtext attention">
                            {failureSentence(item.failure_code)}
                          </span>
                        ) : null}
                        {item.recovered ? (
                          <span className="subtext">
                            Recovered: a later review published
                          </span>
                        ) : null}
                      </th>
                      <td>
                        <span className={`status ${item.state}`}>
                          {states[item.state]}
                        </span>
                      </td>
                      <td>
                        <Phase item={item} />
                      </td>
                      <td>
                        <code title={item.head_sha}>
                          {item.head_sha.slice(0, 8)}
                        </code>
                      </td>
                      <td className="numeric">
                        {item.posted_at !== null
                          ? (item.findings_count ?? "—")
                          : "—"}
                      </td>
                      <td className="numeric">
                        {item.max_attempts === null
                          ? "—"
                          : `${item.attempt_count} of ${item.max_attempts}`}
                      </td>
                      <td>
                        <time
                          className="review-time"
                          dateTime={item.last_heartbeat_at}
                          title={time(item.last_heartbeat_at)}
                        >
                          {since(item.last_heartbeat_at)}
                        </time>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="panel-note">
              Open a request to read its review and execution record. An em dash
              means a value is not recorded.
            </p>
          </section>
        ) : (
          <Empty title="No matching requests">
            Change the filters or request a review on GitHub.
          </Empty>
        ))}
      {query.data &&
      (params.has("before_id") || query.data.next_cursor !== null) ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={!params.has("before_id")}
            onClick={() => update({ before_id: "" })}
          >
            Newest
          </button>
          <span>Up to 50 requests per page</span>
          <button
            className="secondary"
            disabled={query.data.next_cursor === null}
            onClick={() =>
              update({ before_id: String(query.data?.next_cursor) })
            }
          >
            Older requests
          </button>
        </div>
      ) : null}
    </>
  );
}
