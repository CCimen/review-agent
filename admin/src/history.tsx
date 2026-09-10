import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  pixel,
  proportional,
  type TableColumn,
} from "@astryxdesign/core/Table";
import { Selector } from "@astryxdesign/core/Selector";
import { Heading, Text } from "@astryxdesign/core/Text";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useState } from "react";
import {
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { ActivityTabs } from "./activity";
import type {
  HistoryItem,
  PullRequestGroup,
  PullRequestPage,
  ReviewDetail,
} from "./api";
import { APIError, read } from "./api";
import { ReviewFindings } from "./reviewFindings";
import { ReviewProgress, reviewStateLabel } from "./reviewProgress";
import { RunControls } from "./runControls";
import { ScopedLink as Link, useScope } from "./scope";
import {
  Copy,
  duration,
  Empty,
  failureSentence,
  Form,
  Freshness,
  number,
  Period,
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
function Result({ item }: { item: HistoryItem }) {
  return (
    <VStack gap={1}>
      <Text>{reviewStateLabel(item)}</Text>
      {item.posted_at !== null ? (
        <Text type="supporting">
          {item.findings_count === null
            ? "Unknown"
            : number.format(item.findings_count)}{" "}
          finding{item.findings_count === 1 ? "" : "s"}
        </Text>
      ) : item.recovered ? (
        <Text type="supporting">Later review published</Text>
      ) : (
        <ReviewProgress item={item} />
      )}
    </VStack>
  );
}

/** Column widths follow content: the pull request and its supporting line are
 *  the long values, while the timestamp and the action are fixed. Without them
 *  the table sized columns from header text and clipped the repository name. */
function pullRequestColumns(filters: string): TableColumn<PullRequestGroup>[] {
  const hrefFor = (group: PullRequestGroup) =>
    `/history/${group.latest.id}${filters ? `?${filters}` : ""}`;
  return [
    {
      key: "pull_request",
      header: "Pull request",
      width: proportional(2, { minWidth: 280 }),
      renderCell: (group) => (
        <VStack gap={1}>
          <Link to={hrefFor(group)}>
            {group.latest.repository} PR #{group.latest.pr_number}
          </Link>
          <Text color="secondary" type="supporting">
            {number.format(group.matching_requests)} matching request
            {group.matching_requests === 1 ? "" : "s"}
            {group.total_requests !== group.matching_requests
              ? ` · ${number.format(group.total_requests)} total`
              : ""}
            {group.latest.is_latest
              ? " · Latest request"
              : " · Latest matching request"}
          </Text>
        </VStack>
      ),
    },
    {
      key: "result",
      header: "Latest matching result",
      width: proportional(1, { minWidth: 200 }),
      renderCell: (group) => <Result item={group.latest} />,
    },
    {
      key: "started",
      header: "Started",
      width: pixel(168),
      renderCell: (group) => (
        <time dateTime={group.latest.started_at}>
          {time(group.latest.started_at)}
        </time>
      ),
    },
    {
      key: "action",
      header: "Action",
      width: pixel(132),
      renderCell: (group) => (
        <Link to={hrefFor(group)}>
          {group.latest.posted_at !== null ? "View review" : "View request"}
          <VisuallyHidden>
            {" "}
            for {group.latest.repository} PR #{group.latest.pr_number}
          </VisuallyHidden>
        </Link>
      ),
    },
  ];
}

function ReviewOutcome({ item }: { item: HistoryItem }) {
  return (
    <>
      {item.failure_code && (
        <VStack gap={3}>
          <strong>
            {item.recovered
              ? "Earlier failure; a later request published a review."
              : item.is_latest
                ? "The latest review request failed."
                : "This earlier request failed."}
          </strong>
          <Text as="p">
            {failureSentence(item.failure_code)}.{" "}
            <Copy value={item.failure_code} label="failure code">
              <Code>{item.failure_code}</Code>
            </Copy>
            {item.job_failure_code ? (
              <>
                {" "}
                · Worker cause:{" "}
                <Copy value={item.job_failure_code} label="worker failure code">
                  <Code>{item.job_failure_code}</Code>
                </Copy>
              </>
            ) : null}
          </Text>
          <Text as="p">
            Check the review result on GitHub and the operator logs for request
            #{item.id}. After resolving the cause, request <Code>/review</Code>{" "}
            on the PR again.
          </Text>
        </VStack>
      )}
      {item.coverage.state !== "complete" &&
        (item.posted_at !== null ||
          (item.state !== "queued" && item.coverage.registration_complete)) && (
          <Text as="p">
            {item.coverage.state === "unknown"
              ? "Coverage has not been established."
              : `Complete diffs were available for ${item.coverage.changed_paths_with_complete_diff} of ${item.coverage.changed_files_reported ?? "an unknown number of"} changed files.`}{" "}
            A published result may leave changes unreviewed.
          </Text>
        )}
      {(item.publication_superseded || item.state === "superseded") && (
        <Text as="p">
          This review has been superseded. Select a later request to inspect
          more recent recorded results.
        </Text>
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
      <Link to={`/history${backSearch}`}>Back to review history</Link>
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
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <VStack gap={3}>
              <Heading level={1}>
                {item.repository} PR #{item.pr_number}
              </Heading>
              <Text as="p">
                Request #{item.id} · {time(item.started_at)}
                {item.is_latest ? " · Latest request" : " · Earlier request"}
              </Text>
            </VStack>
            <AstryxLink
              href={pullRequestURL(item)}
              target="_blank"
              rel="noreferrer"
            >
              Open pull request
              <VisuallyHidden> on GitHub (opens in a new tab)</VisuallyHidden>
            </AstryxLink>
          </HStack>
          <Grid columns={{ minWidth: 300, max: 2, repeat: "fit" }} gap={6}>
            <VStack as="aside" gap={3} aria-labelledby="request-history-title">
              <Heading level={2} id="request-history-title">
                Review history
              </Heading>
              <Text as="p" color="secondary">
                All retained requests for this PR.
              </Text>
              <Selector
                label={"Selected request"}
                options={[
                  !data.requests.some((request) => request.id === item.id)
                    ? {
                        value: String(item.id),
                        label:
                          "#" +
                          String(item.id) +
                          " · " +
                          time(item.started_at) +
                          " · " +
                          " " +
                          reviewStateLabel(item),
                      }
                    : null,
                  data.requests.map((request) => ({
                    value: String(request.id),
                    label:
                      "#" +
                      String(request.id) +
                      " · " +
                      time(request.started_at) +
                      " · " +
                      " " +
                      reviewStateLabel(request),
                  })),
                ]
                  .flat()
                  .filter((option) => option != null)}
                value={String(item.id)}
                onChange={(value) => navigate(selectedURL(Number(value)))}
              />

              <Table
                density="compact"
                dividers="rows"
                aria-label="Requests, newest first"
              >
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Request</TableHeaderCell>
                    <TableHeaderCell>Result</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.requests.map((request) => (
                    <TableRow key={request.id}>
                      <TableHeaderCell scope="row">
                        <VStack gap={1}>
                          <Link
                            to={selectedURL(request.id)}
                            aria-current={
                              request.id === item.id ? "page" : undefined
                            }
                          >
                            Request #{request.id}
                          </Link>
                          <Text color="secondary" type="supporting">
                            <time dateTime={request.started_at}>
                              {time(request.started_at)}
                            </time>
                          </Text>
                          <Text color="secondary" type="supporting">
                            <Code>{request.head_sha.slice(0, 10)}</Code>
                            {request.previous_head_sha === null
                              ? " · First request"
                              : request.previous_head_sha === request.head_sha
                                ? " · Same head"
                                : " · New head"}
                          </Text>
                        </VStack>
                      </TableHeaderCell>
                      <TableCell>
                        <Result item={request} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>

              {data.requests.length === 0 ? (
                <Text as="p">No older retained requests.</Text>
              ) : null}
              {cursor || data.next_cursor !== null ? (
                <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
                  <Button
                    label={"Newest"}
                    variant="secondary"
                    type="submit"
                    isDisabled={!cursor || query.isFetching}
                    onClick={() => page(null)}
                  />
                  <Button
                    label={"Older"}
                    variant="secondary"
                    type="submit"
                    isDisabled={data.next_cursor === null || query.isFetching}
                    onClick={() => page(data.next_cursor)}
                  />
                </HStack>
              ) : null}
            </VStack>
            <VStack
              gap={4}
              as="section"
              aria-label={`Review request ${item.id}`}
            >
              <VStack gap={4}>
                <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
                  <Result item={item} />
                  {data.can_maintain && <RunControls runId={item.id} />}
                </HStack>
                <Text as="p">
                  Request head{" "}
                  <Copy value={item.head_sha} label="reviewed head SHA">
                    <Code>{item.head_sha.slice(0, 12)}</Code>
                  </Copy>
                  {item.completed_at ? (
                    <Text>
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
                    </Text>
                  ) : null}
                </Text>
                {item.previous_head_sha &&
                item.previous_head_sha !== item.head_sha ? (
                  <AstryxLink
                    href={`https://github.com/${item.repository}/compare/${item.previous_head_sha}...${item.head_sha}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Compare commits with previous request
                    <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
                  </AstryxLink>
                ) : null}
              </VStack>
              <ReviewOutcome item={item} />
              <ReviewFindings runId={item.id} repository={item.repository} />
              {data.markdown !== null ? (
                <>
                  <VStack gap={3}>
                    {data.publication_links.map((link) => (
                      <AstryxLink
                        key={link.url}
                        href={link.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {link.label}
                        <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
                      </AstryxLink>
                    ))}
                  </VStack>
                  <Text as="p">
                    Recorded publication from {time(item.posted_at)}. Later
                    edits and discussion on GitHub are not reflected here.
                  </Text>
                  {data.content_truncated || data.links_truncated ? (
                    <Text as="p">
                      This large review exceeds the reader limit. Open the
                      published review on GitHub for the complete result.
                    </Text>
                  ) : null}
                  <Suspense
                    fallback={
                      <Text as="p" role="status">
                        Loading review text…
                      </Text>
                    }
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
                  item.state === "publishing" ||
                  item.state === "stalled") ? (
                <Text as="p">
                  This page updates automatically as the review progresses.
                </Text>
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
              <Collapsible
                defaultIsOpen={false}
                trigger={
                  <HStack gap={3} wrap="wrap" vAlign="center">
                    Execution details
                  </HStack>
                }
              >
                <VStack gap={4}>
                  <RunDetails item={item} />
                </VStack>
              </Collapsible>
            </VStack>
          </Grid>
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
    <VStack gap={3}>
      <VStack as="dl" gap={2}>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Review request</Text>
          </dt>
          <dd>
            <Copy value={String(item.id)} label="request ID">
              #{item.id}
            </Copy>
          </dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Phase</Text>
          </dt>
          <dd>{item.phase.replaceAll("_", " ")}</dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Head commit</Text>
          </dt>
          <dd>
            <Copy value={item.head_sha} label="head commit SHA">
              <Code>{item.head_sha.slice(0, 12)}</Code>
            </Copy>
          </dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Base commit</Text>
          </dt>
          <dd>
            <Copy value={item.base_sha} label="base commit SHA">
              <Code>{item.base_sha.slice(0, 12)}</Code>
            </Copy>
          </dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Started</Text>
          </dt>
          <dd>{time(item.started_at)}</dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Last heartbeat</Text>
          </dt>
          <dd>{time(item.last_heartbeat_at)}</dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Finished</Text>
          </dt>
          <dd>
            {time(item.completed_at)}
            {elapsed === null
              ? ""
              : elapsed === 0
                ? " · under a second"
                : ` · ${duration(elapsed)}`}
          </dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Worker attempts</Text>
          </dt>
          <dd>
            {item.max_attempts === null
              ? "No durable job recorded"
              : `${item.attempt_count} of ${item.max_attempts}`}
          </dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Changed-file coverage</Text>
          </dt>
          <dd>
            {item.coverage.changed_paths_with_complete_diff} complete diffs of{" "}
            {item.coverage.changed_files_reported ?? "an unknown number of"}{" "}
            {item.coverage.changed_files_reported === 1 ? "file" : "files"}
          </dd>
        </HStack>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <dt>
            <Text color="secondary">Inventory</Text>
          </dt>
          <dd>
            {item.coverage.registration_complete ? "Complete" : "Incomplete"} ·{" "}
            {item.coverage.changed_files_registered}{" "}
            {item.coverage.changed_files_registered === 1 ? "file" : "files"}{" "}
            registered
          </dd>
        </HStack>
      </VStack>
    </VStack>
  );
}

export function History() {
  const scope = useScope();
  const { params, days, update } = useFilters();
  const repository = params.get("repository") ?? "";
  const status = params.get("status") ?? "all";
  const [prDraft, setPrDraft] = useState(params.get("pr_number") ?? "");
  const filtered =
    status !== "all" ||
    params.has("repository") ||
    params.has("pr_number") ||
    params.has("before_id");
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
        <Link to={`/repositories?days=${days}`}>All repositories</Link>
      )}
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>{repository || "Review history"}</Heading>
          <Text as="p">
            Pull requests and their review history, from admission to
            publication.
          </Text>
        </VStack>
      </HStack>
      <ActivityTabs />
      <HStack gap={3} wrap="wrap" vAlign="end">
        <Selector
          label={"Review state"}
          options={[
            { value: "all", label: "All requests" },
            { value: "active", label: "Active now" },
            { value: "published", label: "Published" },
            { value: "failed", label: "Failed" },
            { value: "latest_failed", label: "Latest failures" },
            { value: "superseded", label: "Superseded" },
          ]}
          value={status}
          onChange={(value) => update({ status: value })}
        />
        <Period days={days} change={(value) => update({ days: value })} />
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            update({ pr_number: prDraft });
          }}
        >
          <HStack gap={3} vAlign="end">
            <NumberInput
              isIntegerOnly
              label={"PR number"}
              id="pr-filter"
              min={1}
              hasClear
              placeholder="All PRs"
              value={prDraft ? Number(prDraft) : null}
              onChange={(value) =>
                setPrDraft(value === null ? "" : String(value))
              }
            />

            <Button label={"Filter"} variant="secondary" type="submit" />
          </HStack>
        </Form>
        <Button
          label={"Reset filters"}
          variant="ghost"
          type="button"
          onClick={() => update({ status: "all", pr_number: "", days: "30" })}
        />
      </HStack>
      {status === "active" && (
        <Text as="p">
          Showing all current work, including requests started before the
          reporting period.
        </Text>
      )}
      <Freshness query={query} />
      {query.data && (
        <Text as="p">
          Showing {number.format(query.data.items.length)} of{" "}
          {number.format(query.data.total)} pull request
          {query.data.total === 1 ? "" : "s"}
          {repository ? ` for ${repository}` : ""}.
        </Text>
      )}
      {query.data &&
        (query.data.items.length ? (
          <Table
            aria-label="Pull requests"
            data={query.data.items}
            columns={pullRequestColumns(params.toString())}
            idKey="pull_request_id"
            density="balanced"
            dividers="rows"
            hasHover
            verticalAlign="top"
          />
        ) : (
          <Empty
            title={
              filtered ? "No matching pull requests" : "No pull requests yet"
            }
          >
            {filtered
              ? "Change the state, reporting period, or repository filter."
              : "Pull requests appear here once a review is asked for on GitHub."}
          </Empty>
        ))}
      {query.data &&
        (params.has("before_id") || query.data.next_cursor !== null) && (
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <Button
              label={"Newest pull requests"}
              variant="secondary"
              type="submit"
              isDisabled={!params.has("before_id")}
              onClick={() => update({ before_id: "" })}
            />
            <Text>Newest first · up to 50 per page</Text>
            <Button
              label={"Older pull requests"}
              variant="secondary"
              type="submit"
              isDisabled={query.data.next_cursor === null}
              onClick={() =>
                update({ before_id: String(query.data?.next_cursor) })
              }
            />
          </HStack>
        )}
      <Text as="p">
        A request can include several worker attempts. Review states and
        findings describe the recorded commit. Open a review to read its
        published result. Request another review on GitHub.
      </Text>
    </>
  );
}
