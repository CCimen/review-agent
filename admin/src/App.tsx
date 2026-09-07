import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { APIError, read, write } from "./api";
import type { Account, RepositoryPage } from "./api";

import { Login, MyAccount, Users } from "./accounts";
import { OverviewPage } from "./overview";
import { OperationsPage } from "./operations";
import { History, ReviewPage } from "./history";
import { Empty, Freshness, Period, Stat, number, time, useFilters } from "./ui";

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
  const totals = query.data?.totals;
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
      {query.data && totals && (
        <>
          <div className="stat-grid">
            <Stat label="Repositories" value={query.data.total} />
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
            Totals cover all {query.data.total}{" "}
            {query.data.total === 1 ? "repository" : "repositories"}
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
          <Empty
            title={search ? "No matching repositories" : "No repositories yet"}
          >
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

export function App() {
  const client = useQueryClient();
  const { pathname } = useLocation();
  const main = useRef<HTMLElement>(null);
  const nav = useRef<HTMLElement>(null);
  const me = useQuery({
    queryKey: ["me"],
    queryFn: ({ signal }) => read<Account>("/api/me", signal),
    retry: false,
  });
  const signedOut = me.error instanceof APIError && me.error.status === 401;
  useEffect(() => {
    main.current?.focus({ preventScroll: true });
  }, [pathname, me.data?.id]);
  // The tab strip scrolls on narrow screens; keep the current page in view so
  // landing on a later route still shows where you are.
  useEffect(() => {
    const strip = nav.current;
    const active = strip?.querySelector<HTMLElement>("a.active");
    if (!strip || !active) return;
    const past = active.offsetLeft + active.offsetWidth;
    if (
      past > strip.scrollLeft + strip.clientWidth ||
      active.offsetLeft < strip.scrollLeft
    )
      strip.scrollLeft = Math.max(0, active.offsetLeft - 16);
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
        <nav aria-label="Main navigation" ref={nav}>
          <NavLink to="/" end>
            Overview
          </NavLink>
          <NavLink to="/repositories">Repositories</NavLink>
          <NavLink to="/history">Review history</NavLink>
          {current.role === "admin" && (
            <NavLink to="/operations">Operations</NavLink>
          )}
          {current.role === "admin" && <NavLink to="/users">Users</NavLink>}
        </nav>
        <div className="account-nav">
          <Link to="/account">Your account</Link>
          <button
            className="text-button quiet"
            disabled={logout.isPending}
            onClick={() => logout.mutate()}
          >
            {logout.isPending ? "Signing out…" : "Sign out"}
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
          <Route path="/" element={<OverviewPage />} />
          <Route path="/repositories" element={<Repositories />} />
          <Route path="/history" element={<History />} />
          <Route path="/history/:runId" element={<ReviewPage />} />
          <Route
            path="/operations"
            element={
              current.role === "admin" ? (
                <OperationsPage />
              ) : (
                <Navigate to="/" replace />
              )
            }
          />
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
