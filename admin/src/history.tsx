import { ScopedLink as Link, useScope } from "./scope";
import { RunControls } from "./runControls";
import { ReviewFindings } from "./reviewFindings";
import { lazy, Suspense, useEffect, useState } from "react";
import {
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { APIError, read } from "./api";
import { ActivityTabs } from "./activity";
import type {
  HistoryItem,
  PullRequestGroup,
  PullRequestPage,
  ReviewDetail,
} from "./api";
import {
  Copy,
  Empty,
  Freshness,
  Period,
  duration,
  failureSentence,
  number,
  time,
  useFilters,
} from "./ui";
const ReviewMarkdown = lazy(() =>
  import("./reviewMarkdown").then((module) => ({
    default: module.ReviewMarkdown,
  })),
);

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

function Result({ item }: { item: HistoryItem }) {
  return (
    <span className="review-result">
      <span className={`status ${item.state}`}>{stateLabels[item.state]}</span>
      <span className="subtext">
        {item.posted_at !== null
          ? `${item.findings_count === null ? "Unknown" : number.format(item.findings_count)} finding${item.findings_count === 1 ? "" : "s"}`
          : item.recovered
            ? "Later review published"
            : item.state === "failed"
              ? failureSentence(item.failure_code ?? "")
              : item.state === "superseded"
                ? "A newer request replaced this run"
                : item.state === "queued"
                  ? item.quota_wait_until
                    ? "Waiting for account quota"
                    : "Waiting for a worker"
                  : item.state === "running"
                    ? "Review in progress"
                    : item.state === "publishing"
                      ? "Delivering the result to GitHub"
                      : "No published result yet"}
        {item.coverage.state !== "complete" && item.posted_at !== null ? (
          <span className="coverage-label">
            {" "}
            ·{" "}
            {item.coverage.state === "unknown"
              ? "Coverage unknown"
              : "Limited coverage"}
          </span>
        ) : null}
      </span>
    </span>
  );
}

function PullRequestRow({
  group,
  filters,
}: {
  group: PullRequestGroup;
  filters: string;
}) {
  const item = group.latest;
  const href = `/history/${item.id}${filters ? `?${filters}` : ""}`;
  return (
    <section className="pr-group">
      <div className="review-row-head">
        <span className="request-identity">
          <Link className="run-name" to={href}>
            {item.repository} PR #{item.pr_number}
          </Link>
          <span className="subtext">
            {number.format(group.matching_requests)} matching request
            {group.matching_requests === 1 ? "" : "s"}
            {group.total_requests !== group.matching_requests
              ? ` · ${number.format(group.total_requests)} total`
              : ""}
            {item.is_latest
              ? " · Latest request"
              : " · Latest matching request"}
          </span>
        </span>
        <Result item={item} />
        <time className="review-time" dateTime={item.started_at}>
          {time(item.started_at)}
        </time>
        <Link className="review-open" to={href}>
          {item.posted_at !== null ? "View review" : "View request"}
          <span className="sr-only">
            {" "}
            for {item.repository} PR #{item.pr_number}
          </span>
        </Link>
      </div>
    </section>
  );
}

function ReviewOutcome({ item }: { item: HistoryItem }) {
  return (
    <>
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
            {failureSentence(item.failure_code)}.{" "}
            <Copy value={item.failure_code} label="failure code">
              <code>{item.failure_code}</code>
            </Copy>
            {item.job_failure_code ? (
              <>
                {" "}
                · Worker cause:{" "}
                <Copy value={item.job_failure_code} label="worker failure code">
                  <code>{item.job_failure_code}</code>
                </Copy>
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
      {item.coverage.state !== "complete" &&
        (item.posted_at !== null ||
          (item.state !== "queued" && item.coverage.registration_complete)) && (
          <p className="notice">
            {item.coverage.state === "unknown"
              ? "Coverage has not been established."
              : `Complete diffs were available for ${item.coverage.changed_paths_with_complete_diff} of ${item.coverage.changed_files_reported ?? "an unknown number of"} changed files.`}{" "}
            A published result may leave changes unreviewed.
          </p>
        )}
      {(item.publication_superseded || item.state === "superseded") && (
        <p className="notice">
          This review has been superseded. Select a later request to inspect
          more recent recorded results.
        </p>
      )}
    </>
  );
}

export function ReviewPage() {
  const { runId } = useParams();
  const location = useLocation();
  // Reset disclosure state when another run is selected, including browser Back.
  return (
    <ReviewReader key={runId} runId={runId ?? ""} search={location.search} />
  );
}

function ReviewReader({ runId, search }: { runId: string; search: string }) {
  const scope = useScope();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const before = params.get("requests_before");
  const cursor = before && /^[1-9]\d*$/.test(before) ? before : null;
  const query = useQuery({
    queryKey: ["review", runId, cursor, "scoped", scope.key],
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) =>
      read<ReviewDetail>(
        scope.path(
          `/api/history/${encodeURIComponent(runId)}${cursor ? `?before_id=${cursor}` : ""}`,
        ),
        signal,
      ),
  });
  const item = query.data?.item;
  const backParams = new URLSearchParams(search);
  backParams.delete("requests_before");
  const backSearch = backParams.size ? `?${backParams}` : "";
  useEffect(() => {
    document.title = item
      ? `Review Agent · ${item.repository} PR #${item.pr_number} · Request #${item.id}`
      : "Review Agent · Review";
  }, [item]);
  function page(beforeId: number | null) {
    const next = new URLSearchParams(params);
    if (beforeId === null) next.delete("requests_before");
    else next.set("requests_before", String(beforeId));
    setParams(next, { preventScrollReset: true });
  }
  const selectedURL = (id: number) => `/history/${id}${search}`;
  const data = query.data;
  const missing = query.error instanceof APIError && query.error.status === 404;
  return (
    <>
      <Link className="back-link" to={`/history${backSearch}`}>
        Back to review history
      </Link>
      {missing ? (
        <Empty title="Review request not found">
          It may have been removed by retention. Return to history to find an
          available review.
        </Empty>
      ) : (
        <Freshness query={query} />
      )}
      {!missing && item && data ? (
        <>
          <div className="page-heading review-heading">
            <div>
              <h1>
                {item.repository} PR #{item.pr_number}
              </h1>
              <p>
                Request #{item.id} · {time(item.started_at)}
                {item.is_latest ? " · Latest request" : " · Earlier request"}
              </p>
            </div>
            <a
              className="button secondary"
              href={pullRequestURL(item)}
              target="_blank"
              rel="noreferrer"
            >
              Open pull request
              <span className="sr-only"> on GitHub (opens in a new tab)</span>
            </a>
          </div>
          <div className="review-workspace">
            <aside
              className="request-history"
              aria-labelledby="request-history-title"
            >
              <h2 id="request-history-title">Review history</h2>
              <p className="muted">All retained requests for this PR.</p>
              <label className="field mobile-run-picker">
                Selected request
                <select
                  value={item.id}
                  onChange={(event) =>
                    navigate(selectedURL(Number(event.target.value)))
                  }
                >
                  {!data.requests.some((request) => request.id === item.id) ? (
                    <option value={item.id}>
                      #{item.id} · {time(item.started_at)} ·{" "}
                      {stateLabels[item.state]}
                    </option>
                  ) : null}
                  {data.requests.map((request) => (
                    <option key={request.id} value={request.id}>
                      #{request.id} · {time(request.started_at)} ·{" "}
                      {stateLabels[request.state]}
                    </option>
                  ))}
                </select>
              </label>
              <ol
                className="request-timeline"
                aria-label="Requests, newest first"
              >
                {data.requests.map((request) => (
                  <li key={request.id}>
                    <Link
                      to={selectedURL(request.id)}
                      aria-current={request.id === item.id ? "page" : undefined}
                    >
                      <span className="timeline-identity">
                        <strong>#{request.id}</strong>
                        <time dateTime={request.started_at}>
                          {time(request.started_at)}
                        </time>
                      </span>
                      <Result item={request} />
                      <span className="subtext">
                        <code>{request.head_sha.slice(0, 10)}</code>
                        {request.previous_head_sha === null
                          ? " · First request"
                          : request.previous_head_sha === request.head_sha
                            ? " · Same head"
                            : " · New head"}
                      </span>
                    </Link>
                  </li>
                ))}
              </ol>
              {data.requests.length === 0 ? (
                <p className="notice">No older retained requests.</p>
              ) : null}
              {cursor || data.next_cursor !== null ? (
                <div className="pagination request-pagination">
                  <button
                    className="secondary"
                    disabled={!cursor || query.isFetching}
                    onClick={() => page(null)}
                  >
                    Newest
                  </button>
                  <button
                    className="secondary"
                    disabled={data.next_cursor === null || query.isFetching}
                    onClick={() => page(data.next_cursor)}
                  >
                    Older
                  </button>
                </div>
              ) : null}
            </aside>
            <section
              className="review-reading"
              aria-label={`Review request ${item.id}`}
            >
              <div className="review-summary">
                <div className="review-status-row">
                  <Result item={item} />
                  {data.can_maintain && <RunControls runId={item.id} />}
                </div>
                <p className="review-commit">
                  Request head{" "}
                  <Copy value={item.head_sha} label="reviewed head SHA">
                    <code>{item.head_sha.slice(0, 12)}</code>
                  </Copy>
                  {item.completed_at ? (
                    <span>
                      {" "}
                      ·{" "}
                      {duration(
                        Math.max(
                          0,
                          (Date.parse(item.completed_at) -
                            Date.parse(item.started_at)) /
                            1000,
                        ),
                      )}
                    </span>
                  ) : null}
                </p>
                {item.previous_head_sha &&
                item.previous_head_sha !== item.head_sha ? (
                  <a
                    href={`https://github.com/${item.repository}/compare/${item.previous_head_sha}...${item.head_sha}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Compare commits with previous request
                    <span className="sr-only"> (opens in a new tab)</span>
                  </a>
                ) : null}
              </div>
              <ReviewOutcome item={item} />
              <ReviewFindings runId={item.id} repository={item.repository} />
              {data.markdown !== null ? (
                <>
                  <div className="publication-links">
                    {data.publication_links.map((link) => (
                      <a
                        key={link.url}
                        href={link.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {link.label}
                        <span className="sr-only"> (opens in a new tab)</span>
                      </a>
                    ))}
                  </div>
                  <p className="recorded-note">
                    Recorded publication from {time(item.posted_at)}. Later
                    edits and discussion on GitHub are not reflected here.
                  </p>
                  {data.content_truncated || data.links_truncated ? (
                    <p className="notice">
                      This large review exceeds the reader limit. Open the
                      published review on GitHub for the complete result.
                    </p>
                  ) : null}
                  <Suspense
                    fallback={<p role="status">Loading review text…</p>}
                  >
                    <ReviewMarkdown
                      markdown={data.markdown}
                      repository={item.repository}
                      headSha={item.head_sha}
                    />
                  </Suspense>
                </>
              ) : !item.posted_at &&
                (item.state === "queued" ||
                  item.state === "running" ||
                  item.state === "publishing") ? (
                <p className="review-waiting">
                  {item.quota_wait_until
                    ? `The next quota check is due ${time(item.quota_wait_until)}. Work resumes after the provider confirms quota is available. `
                    : ""}
                  This page updates automatically as the review progresses.
                </p>
              ) : (
                <Empty
                  title={
                    item.state === "failed"
                      ? "No published review"
                      : item.state === "superseded"
                        ? "Request superseded"
                        : item.posted_at
                          ? "Review content unavailable"
                          : "Review not published yet"
                  }
                >
                  {item.state === "failed"
                    ? "This request failed before a complete review was published. The failure information above explains the recorded cause."
                    : item.state === "superseded"
                      ? "This request was replaced before publication. Select a later request from the history."
                      : item.posted_at
                        ? "The retained record does not contain a published review body. Check the pull request on GitHub."
                        : item.state === "publishing"
                          ? "The result is being delivered to GitHub. The reader will update when publication completes."
                          : "The request is queued or still being reviewed. This page updates while you wait."}
                </Empty>
              )}
              <details className="execution-details">
                <summary>Execution details</summary>
                <RunDetails item={item} />
              </details>
            </section>
          </div>
        </>
      ) : null}
    </>
  );
}

function RunDetails({ item }: { item: HistoryItem }) {
  const elapsed = item.completed_at
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
          <dd>
            <Copy value={String(item.id)} label="request ID">
              #{item.id}
            </Copy>
          </dd>
        </div>
        <div>
          <dt>Phase</dt>
          <dd>{item.phase.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Head commit</dt>
          <dd>
            <Copy value={item.head_sha} label="head commit SHA">
              <code>{item.head_sha.slice(0, 12)}</code>
            </Copy>
          </dd>
        </div>
        <div>
          <dt>Base commit</dt>
          <dd>
            <Copy value={item.base_sha} label="base commit SHA">
              <code>{item.base_sha.slice(0, 12)}</code>
            </Copy>
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
            {elapsed === null
              ? ""
              : elapsed === 0
                ? " · under a second"
                : ` · ${duration(elapsed)}`}
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
            {item.coverage.changed_paths_with_complete_diff} complete diffs of{" "}
            {item.coverage.changed_files_reported ?? "an unknown number of"}{" "}
            {item.coverage.changed_files_reported === 1 ? "file" : "files"}
          </dd>
        </div>
        <div>
          <dt>Inventory</dt>
          <dd>
            {item.coverage.registration_complete ? "Complete" : "Incomplete"} ·{" "}
            {item.coverage.changed_files_registered}{" "}
            {item.coverage.changed_files_registered === 1 ? "file" : "files"}{" "}
            registered
          </dd>
        </div>
      </dl>
    </div>
  );
}

export function History() {
  const scope = useScope();
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
    queryKey: ["pull-requests", queryParams.toString(), "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<PullRequestPage>(
        scope.path(`/api/pull-requests?${queryParams}`),
        signal,
      ),
  });
  return (
    <>
      {repository && (
        <Link className="back-link" to={`/repositories?days=${days}`}>
          All repositories
        </Link>
      )}
      <div className="page-heading">
        <div>
          <h1>{repository || "Review history"}</h1>
          <p>
            Pull requests and their review history, from admission to
            publication.
          </p>
        </div>
      </div>
      <ActivityTabs />
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
      {query.data && (
        <p className="result-count">
          Showing {number.format(query.data.items.length)} of{" "}
          {number.format(query.data.total)} pull request
          {query.data.total === 1 ? "" : "s"}
          {repository ? ` for ${repository}` : ""}.
        </p>
      )}
      {query.data &&
        (query.data.items.length ? (
          <div className="review-list panel">
            <div className="list-labels" aria-hidden="true">
              <span>Pull request</span>
              <span>Latest matching result</span>
              <span>Started</span>
            </div>
            {query.data.items.map((group) => (
              <PullRequestRow
                key={`${group.pull_request_id}:${queryParams}`}
                group={group}
                filters={params.toString()}
              />
            ))}
          </div>
        ) : (
          <Empty
            title={
              params.has("repository") ||
              params.has("pr_number") ||
              status !== "all"
                ? "No matching review requests"
                : "No review requests yet"
            }
          >
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
              Newest pull requests
            </button>
            <span>Newest first · up to 50 per page</span>
            <button
              className="secondary"
              disabled={query.data.next_cursor === null}
              onClick={() =>
                update({ before_id: String(query.data?.next_cursor) })
              }
            >
              Older pull requests
            </button>
          </div>
        )}
      <p className="footnote">
        A request can include several worker attempts. Review states and
        findings describe the recorded commit. Open a review to read its
        published result. Request another review on GitHub.
      </p>
    </>
  );
}
