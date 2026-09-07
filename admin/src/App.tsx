import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
  useSearchParams,
} from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import { APIError, read, write } from "./api";
import type { Account, HistoryItem, HistoryPage, RepositoryPage } from "./api";

import { Login, MyAccount, Users } from "./accounts";
import { Stat, number } from "./ui";

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
const time = (value: string | null) =>
  value ? dateTime.format(new Date(value)) : "—";
const pullRequestURL = (item: Pick<HistoryItem, "repository" | "pr_number">) =>
  `https://github.com/${item.repository}/pull/${item.pr_number}`;
const stateLabels: Record<HistoryItem["state"], string> = {
  queued: "Queued",
  running: "Reviewing",
  publishing: "Publishing",
  published: "Published",
  failed: "Failed",
  superseded: "Superseded",
};

function useFilters() {
  const [params, setParams] = useSearchParams();
  const days = [7, 30, 90].includes(Number(params.get("days")))
    ? Number(params.get("days"))
    : 30;
  const update = (values: Record<string, string>) => {
    const next = new URLSearchParams(params);
    next.delete("before_id");
    next.delete("offset");
    for (const [key, value] of Object.entries(values))
      value ? next.set(key, value) : next.delete(key);
    setParams(next);
  };
  return { params, days, update };
}

function Period({
  days,
  change,
}: {
  days: number;
  change: (value: string) => void;
}) {
  return (
    <label className="field">
      Reporting period
      <select value={days} onChange={(event) => change(event.target.value)}>
        <option value="7">Last 7 days</option>
        <option value="30">Last 30 days</option>
        <option value="90">Last 90 days</option>
      </select>
    </label>
  );
}

function Freshness<T>({ query }: { query: UseQueryResult<T, Error> }) {
  return (
    <div className="freshness" aria-live="polite">
      {query.isError ? (
        <div className="notice error" role="alert">
          <p>
            {query.error.message || "Could not connect to Review Agent."}{" "}
            {query.data ? "The last available data is still shown below." : ""}
          </p>
          {query.error instanceof APIError && query.error.status === 401 ? (
            <button onClick={() => window.location.reload()}>
              Reload to sign in
            </button>
          ) : (
            <button onClick={() => void query.refetch()}>Retry</button>
          )}
        </div>
      ) : (
        <span>
          {query.isPending
            ? "Loading review data…"
            : `Updated ${time(new Date(query.dataUpdatedAt).toISOString())} · refreshes every 10 seconds`}
        </span>
      )}
    </div>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <h2>No matching activity</h2>
      <p>{children}</p>
    </div>
  );
}

function Repositories() {
  const { params, days, update } = useFilters();
  const search = params.get("search") ?? "";
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const [draft, setDraft] = useState(search);
  useEffect(() => {
    setDraft(search);
  }, [search]);
  useEffect(() => {
    document.title = "Review Agent · Repositories";
  }, []);
  const queryParams = new URLSearchParams({
    days: String(days),
    search,
    offset: String(offset),
    limit: "50",
  });
  const query = useQuery({
    queryKey: ["repositories", queryParams.toString()],
    queryFn: ({ signal }) =>
      read<RepositoryPage>(`/api/repositories?${queryParams}`, signal),
  });
  const totals = (query.data?.items ?? []).reduce(
    (sum, repo) => ({
      prs_reviewed: sum.prs_reviewed + repo.prs_reviewed,
      published_requests: sum.published_requests + repo.published_requests,
      failed_requests: sum.failed_requests + repo.failed_requests,
      active_requests: sum.active_requests + repo.active_requests,
      latest_failed_prs: sum.latest_failed_prs + repo.latest_failed_prs,
    }),
    {
      prs_reviewed: 0,
      published_requests: 0,
      failed_requests: 0,
      active_requests: 0,
      latest_failed_prs: 0,
    },
  );
  function submit(event: FormEvent) {
    event.preventDefault();
    update({ search: draft.trim() });
  }
  function historyURL(repository: string, status = "all") {
    return `/history?${new URLSearchParams({ repository, days: String(days), status })}`;
  }
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Repositories</h1>
          <p>Review activity across your registered repositories.</p>
        </div>
        <Link className="button secondary" to={`/history?days=${days}`}>
          View all reviews
        </Link>
      </div>
      <div className="toolbar">
        <form className="search-form" onSubmit={submit}>
          <label className="field grow" htmlFor="repo-search">
            Find a repository
            <input
              id="repo-search"
              type="search"
              placeholder="Search owner or repository"
              value={draft}
              maxLength={200}
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <button type="submit">Search</button>
        </form>
        <Period days={days} change={(value) => update({ days: value })} />
      </div>
      <Freshness query={query} />
      {query.data && query.data.items.length > 0 && (
        <>
          <div className="stat-grid">
            <Stat label="Repositories" value={query.data.items.length} />
            <Stat label="PRs reviewed" value={totals.prs_reviewed} />
            <Stat label="Published reviews" value={totals.published_requests} />
            <Stat label="Failed requests" value={totals.failed_requests} />
            <Stat label="Active now" value={totals.active_requests} />
            <Stat
              label="Needs attention"
              value={totals.latest_failed_prs}
              attention
            />
          </div>
          <p className="stat-note">
            {`Totals cover ${
              query.data.has_more || offset > 0 ? "the" : "all"
            } ${query.data.items.length} ${
              query.data.items.length === 1 ? "repository" : "repositories"
            }${query.data.has_more || offset > 0 ? " on this page" : ""}`}
            {search ? ` matching “${search}”` : ""} over the last {days} days.
            “Needs attention” counts PRs whose most recent request failed.
          </p>
        </>
      )}
      {query.data &&
        (query.data.items.length ? (
          <div className="panel">
            <div
              className="table-scroll"
              tabIndex={0}
              role="region"
              aria-label="Repository statistics"
            >
              {/* Keep table semantics when the rows reflow on small screens. */}
              <table className="repository-table" role="table">
                <caption>
                  Requests started in the last {days} days. Active work is the
                  current total.
                </caption>
                <thead role="rowgroup">
                  <tr role="row">
                    <th scope="col" role="columnheader">
                      Repository
                    </th>
                    <th scope="col" role="columnheader" className="numeric">
                      PRs reviewed
                    </th>
                    <th scope="col" role="columnheader" className="numeric">
                      Published reviews
                    </th>
                    <th scope="col" role="columnheader" className="numeric">
                      Failed requests
                    </th>
                    <th scope="col" role="columnheader" className="numeric">
                      Active now
                    </th>
                    <th scope="col" role="columnheader">
                      Latest failures
                    </th>
                  </tr>
                </thead>
                <tbody role="rowgroup">
                  {query.data.items.map((repo) => (
                    <tr key={repo.repository} role="row">
                      <th scope="row" role="rowheader">
                        <Link to={historyURL(repo.repository)}>
                          {repo.repository}
                        </Link>
                        <span className="subtext">
                          {repo.last_activity_at
                            ? `Last activity ${time(repo.last_activity_at)}`
                            : "No activity in this period"}
                        </span>
                      </th>
                      <td className="numeric" role="cell">
                        <span className="mobile-label" aria-hidden="true">
                          PRs reviewed
                        </span>
                        <span className="metric-value">
                          {number.format(repo.prs_reviewed)}
                        </span>
                      </td>
                      <td className="numeric" role="cell">
                        <span className="mobile-label" aria-hidden="true">
                          Published reviews
                        </span>
                        <Link
                          aria-label={`${repo.published_requests} published reviews for ${repo.repository}`}
                          to={historyURL(repo.repository, "published")}
                        >
                          {number.format(repo.published_requests)}
                        </Link>
                      </td>
                      <td className="numeric" role="cell">
                        <span className="mobile-label" aria-hidden="true">
                          Failed requests
                        </span>
                        <Link
                          aria-label={`${repo.failed_requests} failed requests for ${repo.repository}`}
                          to={historyURL(repo.repository, "failed")}
                        >
                          {number.format(repo.failed_requests)}
                        </Link>
                      </td>
                      <td className="numeric" role="cell">
                        <span className="mobile-label" aria-hidden="true">
                          Active now
                        </span>
                        <Link
                          aria-label={`${repo.active_requests} active reviews for ${repo.repository}`}
                          to={historyURL(repo.repository, "active")}
                        >
                          {number.format(repo.active_requests)}
                        </Link>
                      </td>
                      <td className="latest-failures" role="cell">
                        <span className="mobile-label" aria-hidden="true">
                          Latest failures
                        </span>
                        {repo.latest_failed_prs ? (
                          <Link
                            className="attention"
                            to={historyURL(repo.repository, "latest_failed")}
                          >
                            {number.format(repo.latest_failed_prs)} PR
                            {repo.latest_failed_prs === 1 ? "" : "s"} to check
                          </Link>
                        ) : (
                          <span className="muted">None in period</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <Empty>
            {search ? (
              <>
                No repository matches “{search}”. Try another name, or{" "}
                <button
                  className="text-button inline"
                  onClick={() => update({ search: "" })}
                >
                  clear the search
                </button>
                .
              </>
            ) : (
              "Repositories appear after they have been registered by Review Agent. Request a review on GitHub to create review activity."
            )}
          </Empty>
        ))}
      {query.data && (offset > 0 || query.data.has_more) && (
        <div className="pagination">
          <button
            className="secondary"
            disabled={offset === 0}
            onClick={() => update({ offset: String(Math.max(0, offset - 50)) })}
          >
            Previous
          </button>
          <span>Page {Math.floor(offset / 50) + 1}</span>
          <button
            className="secondary"
            disabled={!query.data.has_more || offset >= 10000}
            onClick={() => update({ offset: String(offset + 50) })}
          >
            Next
          </button>
        </div>
      )}
      <details className="metric-note">
        <summary>How these counts work</summary>
        <p>
          <strong>PRs reviewed</strong> counts distinct PRs with a published
          review from a request started in this period.{" "}
          <strong>Published reviews</strong> counts those requests, including
          repeat reviews. <strong>Failed requests</strong> includes failures
          later recovered by another request. <strong>Latest failures</strong>{" "}
          counts PRs whose most recent request failed in this period.
        </p>
        <p>
          Publication means a result reached GitHub. It does not mean the PR is
          approved, defect-free, or fully covered. These statistics reflect
          retained Review Agent records.
        </p>
      </details>
    </>
  );
}

function RunDetails({ item }: { item: HistoryItem }) {
  const duration = item.completed_at
    ? Math.max(
        0,
        Math.round(
          (Date.parse(item.completed_at) - Date.parse(item.started_at)) / 1000,
        ),
      )
    : null;
  return (
    <div className="run-details">
      <dl>
        <div>
          <dt>Review request</dt>
          <dd>#{item.id}</dd>
        </div>
        <div>
          <dt>Phase</dt>
          <dd>{item.phase.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Head commit</dt>
          <dd>
            <code>{item.head_sha}</code>
          </dd>
        </div>
        <div>
          <dt>Base commit</dt>
          <dd>
            <code>{item.base_sha}</code>
          </dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{time(item.started_at)}</dd>
        </div>
        <div>
          <dt>Last heartbeat</dt>
          <dd>{time(item.last_heartbeat_at)}</dd>
        </div>
        <div>
          <dt>Finished</dt>
          <dd>
            {time(item.completed_at)}
            {duration !== null ? ` · ${number.format(duration)} seconds` : ""}
          </dd>
        </div>
        <div>
          <dt>Worker attempts</dt>
          <dd>
            {item.max_attempts === null
              ? "No durable job recorded"
              : `${item.attempt_count} of ${item.max_attempts}`}
          </dd>
        </div>
        <div>
          <dt>Changed-file coverage</dt>
          <dd>
            {item.coverage.changed_paths_with_complete_diff} complete diffs /{" "}
            {item.coverage.changed_files_reported ?? "unknown"} reported files
          </dd>
        </div>
        <div>
          <dt>Inventory</dt>
          <dd>
            {item.coverage.registration_complete ? "Complete" : "Incomplete"} ·{" "}
            {item.coverage.changed_files_registered} files registered
          </dd>
        </div>
      </dl>
      {item.coverage.state !== "complete" && (
        <p className="notice">
          {item.coverage.state === "unknown"
            ? "Coverage has not been established."
            : "Changed-file coverage is incomplete."}{" "}
          A published result may leave changes unreviewed.
        </p>
      )}
      {item.failure_code && (
        <div className="notice error">
          <strong>
            {item.recovered
              ? "Earlier failure; a later request published a review."
              : item.is_latest
                ? "The latest review request failed."
                : "This earlier request failed."}
          </strong>
          <p>
            Recorded cause: <code>{item.failure_code}</code>
            {item.job_failure_code ? (
              <>
                {" "}
                · Worker cause: <code>{item.job_failure_code}</code>
              </>
            ) : null}
          </p>
          <p>
            Check the review result on GitHub and the operator logs for request
            #{item.id}. After resolving the cause, request <code>/review</code>{" "}
            on the PR again.
          </p>
        </div>
      )}
      {(item.publication_superseded || item.state === "superseded") && (
        <p className="notice">
          This review has been superseded. Use the latest review for the current
          PR.
        </p>
      )}
      <a href={pullRequestURL(item)} target="_blank" rel="noreferrer">
        Open PR and review results on GitHub
        <span className="sr-only"> (opens in a new tab)</span>
      </a>
    </div>
  );
}

function ReviewRow({
  item,
  repositoryHref,
}: {
  item: HistoryItem;
  repositoryHref: string | null;
}) {
  const [open, setOpen] = useState(false);
  const panelId = `review-${item.id}`;
  return (
    <div className={open ? "review-row open" : "review-row"}>
      <div className="review-row-head">
        <span className="request-identity">
          <span className="run-name">
            {repositoryHref && (
              <>
                <Link to={repositoryHref}>{item.repository}</Link>{" "}
              </>
            )}
            <a href={pullRequestURL(item)} target="_blank" rel="noreferrer">
              {repositoryHref ? `#${item.pr_number}` : `PR #${item.pr_number}`}
              <span className="sr-only"> on GitHub (opens in a new tab)</span>
            </a>
          </span>
          <span className="subtext">
            Request #{item.id} · <code>{item.head_sha.slice(0, 10)}</code>
            {!item.is_latest ? " · Earlier request" : ""}
          </span>
        </span>
        <span className="review-result">
          <span className={`status ${item.state}`}>
            {stateLabels[item.state]}
          </span>
          <span className="subtext">
            {item.state === "published"
              ? `${item.findings_count ?? "Unknown"} findings${item.coverage.state !== "complete" ? " · Limited coverage" : ""}`
              : item.recovered
                ? "Later review published"
                : item.state === "failed" && item.is_latest
                  ? "Latest request · check cause"
                  : item.phase.replaceAll("_", " ")}
          </span>
        </span>
        <time className="review-time" dateTime={item.started_at}>
          <span className="mobile-label">Started </span>
          {time(item.started_at)}
        </time>
        <button
          className="expand-button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((value) => !value)}
        >
          Details
          <span className="sr-only">{` for request #${item.id}`}</span>
        </button>
      </div>
      <div id={panelId} hidden={!open}>
        <RunDetails item={item} />
      </div>
    </div>
  );
}

function History() {
  const { params, days, update } = useFilters();
  const repository = params.get("repository") ?? "";
  const status = params.get("status") ?? "all";
  const [prDraft, setPrDraft] = useState(params.get("pr_number") ?? "");
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
    document.title = `Review Agent · ${repository || "Review history"}`;
  }, [repository]);
  useEffect(() => {
    setPrDraft(params.get("pr_number") ?? "");
  }, [params]);
  const query = useQuery({
    queryKey: ["history", queryParams.toString()],
    queryFn: ({ signal }) =>
      read<HistoryPage>(`/api/history?${queryParams}`, signal),
  });
  function repositoryURL(name: string) {
    return `/history?${new URLSearchParams({ repository: name, days: String(days), status })}`;
  }
  return (
    <>
      <Link className="back-link" to={`/?days=${days}`}>
        All repositories
      </Link>
      <div className="page-heading">
        <div>
          <h1>{repository || "Review history"}</h1>
          <p>Every review request, from admission to its published result.</p>
        </div>
      </div>
      <div className="toolbar history-toolbar">
        <label className="field">
          Review state
          <select
            value={status}
            onChange={(event) => update({ status: event.target.value })}
          >
            <option value="all">All requests</option>
            <option value="active">Active now</option>
            <option value="published">Published</option>
            <option value="failed">Failed</option>
            <option value="latest_failed">Latest failures</option>
            <option value="superseded">Superseded</option>
          </select>
        </label>
        <Period days={days} change={(value) => update({ days: value })} />
        <form
          className="search-form"
          onSubmit={(event) => {
            event.preventDefault();
            update({ pr_number: prDraft });
          }}
        >
          <label className="field" htmlFor="pr-filter">
            PR number
            <input
              id="pr-filter"
              type="number"
              min="1"
              placeholder="All PRs"
              value={prDraft}
              onChange={(event) => setPrDraft(event.target.value)}
            />
          </label>
          <button type="submit">Filter</button>
        </form>
        <button
          className="text-button"
          onClick={() => update({ status: "all", pr_number: "", days: "30" })}
        >
          Reset filters
        </button>
      </div>
      {status === "active" && (
        <p className="notice">
          Showing all current work, including requests started before the
          reporting period.
        </p>
      )}
      <Freshness query={query} />
      {query.data && query.data.items.length > 0 && (
        <p className="result-count">
          {number.format(query.data.items.length)} review request
          {query.data.items.length === 1 ? "" : "s"}
          {repository ? ` for ${repository}` : ""}
          {query.data.next_cursor !== null ? " on this page" : ""}
        </p>
      )}
      {query.data &&
        (query.data.items.length ? (
          <div className="review-list panel">
            <div className="list-labels" aria-hidden="true">
              <span>Pull request / review request</span>
              <span>Result</span>
              <span>Started</span>
            </div>
            {query.data.items.map((item) => (
              <ReviewRow
                key={item.id}
                item={item}
                repositoryHref={
                  repository ? null : repositoryURL(item.repository)
                }
              />
            ))}
          </div>
        ) : (
          <Empty>
            Change the state or reporting period, or request a review on GitHub.
          </Empty>
        ))}
      {query.data &&
        (params.has("before_id") || query.data.next_cursor !== null) && (
          <div className="pagination">
            <button
              className="secondary"
              disabled={!params.has("before_id")}
              onClick={() => update({ before_id: "" })}
            >
              Newest requests
            </button>
            <span>Newest first · up to 50 per page</span>
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
        )}
      <p className="footnote">
        A request can include several worker attempts. Review states and
        findings describe the recorded commit. GitHub remains the place to read
        results and request another review.
      </p>
    </>
  );
}

export function App() {
  const client = useQueryClient();
  const { pathname } = useLocation();
  const main = useRef<HTMLElement>(null);
  const me = useQuery({
    queryKey: ["me"],
    queryFn: ({ signal }) => read<Account>("/api/me", signal),
    retry: false,
  });
  const signedOut = me.error instanceof APIError && me.error.status === 401;
  useEffect(() => {
    main.current?.focus();
  }, [pathname, me.data?.id]);
  useEffect(() => {
    if (signedOut)
      client.removeQueries({
        predicate: (query) => query.queryKey[0] !== "me",
      });
  }, [signedOut, client]);
  const logout = useMutation({
    mutationFn: () => write("/api/auth/logout", "POST"),
    onSuccess: async () => {
      client.removeQueries({
        predicate: (query) => query.queryKey[0] !== "me",
      });
      await client.resetQueries({ queryKey: ["me"] });
    },
  });
  if (signedOut) return <Login />;
  if (!me.data)
    return (
      <main className="login-page">
        <div className="login-card">
          <span className="brand">
            <span className="brand-mark" aria-hidden="true">
              RA
            </span>
            Review Agent
          </span>
          <Freshness query={me} />
        </div>
      </main>
    );
  const current = me.data;
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <Link className="brand" to="/">
          <span className="brand-mark" aria-hidden="true">
            RA
          </span>
          Review Agent
        </Link>
        <nav aria-label="Main navigation">
          <NavLink to="/" end>
            Repositories
          </NavLink>
          <NavLink to="/history">Review history</NavLink>
          {current.role === "admin" && <NavLink to="/users">Users</NavLink>}
        </nav>
        <div className="account-nav">
          <Link to="/account">Your account</Link>
          <button
            className="text-button"
            disabled={logout.isPending}
            onClick={() => logout.mutate()}
          >
            Sign out
          </button>
        </div>
      </header>
      <main id="main" ref={main} tabIndex={-1}>
        {logout.isError && (
          <p className="notice error" role="alert">
            Could not sign out. Please try again.
          </p>
        )}
        <Routes>
          <Route path="/" element={<Repositories />} />
          <Route path="/history" element={<History />} />
          <Route
            path="/users"
            element={
              current.role === "admin" ? (
                <Users current={current} />
              ) : (
                <Navigate to="/" replace />
              )
            }
          />
          <Route path="/account" element={<MyAccount current={current} />} />
        </Routes>
      </main>
      <footer>Review Agent · Advisory pull-request reviews</footer>
    </>
  );
}
