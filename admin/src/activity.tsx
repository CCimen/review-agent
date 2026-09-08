import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/CodeBlock";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import {
  SegmentedControl,
  SegmentedControlItem,
} from "@astryxdesign/core/SegmentedControl";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import {
  Table,
  pixel,
  proportional,
  type TableColumn,
} from "@astryxdesign/core/Table";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useQuery } from "@tanstack/react-query";
import type { InputHTMLAttributes } from "react";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import type { HistoryItem, HistoryPage, Overview } from "./api";
import { read } from "./api";
import { ScopedLink as Link, ScopedAnchor, useScope } from "./scope";
import {
  Empty,
  Form,
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
  const { pathname } = useLocation();
  const value = pathname.startsWith("/history")
    ? "/history"
    : pathname === "/overview"
      ? "/overview"
      : "/";
  return (
    <TabList
      aria-label="Activity views"
      value={value}
      // ScopedAnchor navigates; the current route owns the selected tab.
      onChange={() => {}}
      hasDivider
    >
      <Tab value="/" href="/" as={ScopedAnchor} label="Requests" />
      <Tab
        value="/history"
        href="/history"
        as={ScopedAnchor}
        label="Pull requests"
      />
      <Tab
        value="/overview"
        href="/overview"
        as={ScopedAnchor}
        label="Statistics"
      />
    </TabList>
  );
}

export function Phase({ item }: { item: HistoryItem }) {
  const index = phases.indexOf(item.phase);
  return (
    <VStack gap={1}>
      {item.state === "running" || item.state === "publishing" ? (
        <ProgressBar
          value={index + 1}
          max={phases.length}
          label="Review progress"
          isLabelHidden
        />
      ) : null}
      <Text>{phaseLabels[item.phase] ?? item.phase.replaceAll("_", " ")}</Text>
    </VStack>
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
  const columns: TableColumn<HistoryItem>[] = [
    {
      key: "repository",
      header: "Pull request",
      width: proportional(2, { minWidth: 250 }),
      renderCell: (item) => (
        <VStack gap={1}>
          <Link to={`/history/${item.id}?${params}`}>
            {item.repository} <Text>#{item.pr_number}</Text>
          </Link>
          <Text type="supporting">
            Request #{item.id} · {time(item.started_at)}
          </Text>
          {item.failure_code ? (
            <Text type="supporting">{failureSentence(item.failure_code)}</Text>
          ) : null}
          {item.recovered ? (
            <Text type="supporting">Recovered: a later review published</Text>
          ) : null}
        </VStack>
      ),
    },
    {
      key: "state",
      header: "State",
      width: pixel(126),
      renderCell: (item) => (
        <HStack gap={2}>
          <StatusDot
            label={states[item.state]}
            aria-hidden="true"
            variant={
              item.state === "published"
                ? "success"
                : item.state === "failed"
                  ? "error"
                  : item.state === "running" || item.state === "publishing"
                    ? "accent"
                    : "neutral"
            }
          />
          <Text>{states[item.state]}</Text>
        </HStack>
      ),
    },
    {
      key: "phase",
      header: "Progress",
      width: proportional(1, { minWidth: 170 }),
      renderCell: (item) => <Phase item={item} />,
    },
    {
      key: "head_sha",
      header: "Commit",
      width: pixel(100),
      renderCell: (item) => (
        <abbr title={item.head_sha}>
          <Code>{item.head_sha.slice(0, 8)}</Code>
        </abbr>
      ),
    },
    {
      key: "findings_count",
      header: "Findings",
      align: "end",
      width: pixel(90),
      renderCell: (item) =>
        item.posted_at !== null ? (item.findings_count ?? "—") : "—",
    },
    {
      key: "attempt_count",
      header: "Attempts used",
      align: "end",
      width: pixel(124),
      renderCell: (item) =>
        item.max_attempts === null
          ? "—"
          : `${item.attempt_count} of ${item.max_attempts}`,
    },
    {
      key: "last_heartbeat_at",
      header: "Last activity",
      width: pixel(150),
      renderCell: (item) => (
        <time
          dateTime={item.last_heartbeat_at}
          title={time(item.last_heartbeat_at)}
        >
          {since(item.last_heartbeat_at)}
        </time>
      ),
    },
  ];
  return (
    <VStack gap={6}>
      <HStack justify="between" align="end" wrap="wrap" gap={4}>
        <VStack gap={2}>
          <Heading level={1}>Activity</Heading>
          <Text color="secondary">
            Follow review requests and open their results.
          </Text>
        </VStack>
        <Period days={days} change={(value) => update({ days: value })} />
      </HStack>
      <ActivityTabs />
      <Grid columns={{ minWidth: 180, max: 4 }} gap={6}>
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
            value={data.live_review_workers}
            hint={`${data.review_capacity} slots reported by online workers`}
          />
        ) : null}
      </Grid>
      <Freshness query={overview} quiet />
      <VStack gap={4}>
        <HStack gap={4} justify="between" align="end" wrap="wrap">
          <SegmentedControl
            label="Filter request state"

            value={status}
            onChange={(value) => update({ status: value })}
          >
            {(
              [
                ["all", "All requests"],
                ["active", "Active"],
                ["published", "Published"],
                ["failed", "Failed"],
              ] as const
            ).map(([value, label]) => (
              <SegmentedControlItem key={value} value={value} label={label} />
            ))}
          </SegmentedControl>
          <Form
            onSubmit={(event) => {
              event.preventDefault();
              update({ repository: repository.trim(), pr_number: pr });
            }}
          >
            <HStack gap={3} align="end" wrap="wrap">
              <TextInput
                label={"Repository"}
                id="activity-repo"
                placeholder="owner/repository (optional)"
                value={repository}
                onChange={(value) => setRepository(value)}
                {...({
                  maxLength: 200,
                } satisfies InputHTMLAttributes<HTMLInputElement>)}
              />

              <NumberInput
                isIntegerOnly
                label={"PR number"}
                id="activity-pr"
                min={1}
                hasClear
                placeholder="Any"
                value={pr ? Number(pr) : null}
                onChange={(value) => setPr(value === null ? "" : String(value))}
              />

              <Button label="Filter" type="submit" />
              {params.has("repository") ||
              params.has("pr_number") ||
              params.has("before_id") ? (
                <Button
                  label="Clear filters"
                  variant="ghost"
                  onClick={() =>
                    update({ repository: "", pr_number: "", before_id: "" })
                  }
                />
              ) : null}
            </HStack>
          </Form>
        </HStack>
        <Freshness query={query} />
        {query.data &&
          (query.data.items.length ? (
            <VStack gap={3}>
              <HStack gap={3} justify="between" wrap="wrap" align="center">
                <Heading level={2}>Review requests</Heading>
                <Text type="supporting">
                  {number.format(query.data.total)} matching ·{" "}
                  {query.data.items.length} shown
                </Text>
              </HStack>
              <Table
                aria-label="Review requests"
                data={query.data.items}
                columns={columns}
                idKey="id"
                density="balanced"
                dividers="rows"
                hasHover
                verticalAlign="top"
              />
              <Text type="supporting">
                Open a request to read its review and execution record. An em
                dash means a value is not recorded.
              </Text>
            </VStack>
          ) : (
            <Empty title="No matching requests">
              Change the filters or request a review on GitHub.
            </Empty>
          ))}
        {query.data &&
        (params.has("before_id") || query.data.next_cursor !== null) ? (
          <HStack gap={3} justify="between" wrap="wrap" align="center">
            <Button
              label="Newest"
              isDisabled={!params.has("before_id")}
              onClick={() => update({ before_id: "" })}
            />
            <Text type="supporting">Up to 50 requests per page</Text>
            <Button
              label="Older requests"
              isDisabled={query.data.next_cursor === null}
              onClick={() =>
                update({ before_id: String(query.data?.next_cursor) })
              }
            />
          </HStack>
        ) : null}
      </VStack>
    </VStack>
  );
}
