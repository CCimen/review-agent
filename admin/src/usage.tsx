import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import {
  Table,
  pixel,
  proportional,
  type TableColumn,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useQuery } from "@tanstack/react-query";
import { useEffect, type InputHTMLAttributes } from "react";
import { Navigate } from "react-router-dom";
import type { UsageReport, UsageRow } from "./api";
import { read } from "./api";
import { ScopedLink as Link, isAdmin, useScope } from "./scope";
import {
  Empty,
  Freshness,
  Period,
  Prose,
  Stat,
  Unknown,
  number,
  time,
  useFilters,
  useLiveSearch,
} from "./ui";

const dimensions: { value: UsageReport["dimension"]; label: string }[] = [
  { value: "team", label: "Teams" },
  { value: "repository", label: "Repositories" },
  { value: "requester", label: "GitHub users" },
];
const percent = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumFractionDigits: 1,
});
const pageSize = 25;

export function UsagePage() {
  return isAdmin(useScope().current.role) ? (
    <UsageContent />
  ) : (
    <Navigate to="/" replace />
  );
}

function UsageContent() {
  const scope = useScope();
  const { params, days, update } = useFilters();
  const requestedDimension = params.get("dimension");
  const dimension =
    requestedDimension === "team" || requestedDimension === "requester"
      ? requestedDimension
      : "repository";
  const requestedSort = params.get("sort");
  const sort =
    requestedSort === "total_tokens" || requestedSort === "published_requests"
      ? requestedSort
      : "requests";
  const repository = params.get("repository") ?? "";
  const search = params.get("search") ?? "";
  const offset = Math.min(
    10000,
    Math.max(0, Math.floor(Number(params.get("offset")) || 0)),
  );
  const { draft, setDraft, flush } = useLiveSearch(search, (value) =>
    update({ search: value }, { replace: true }),
  );
  const customPeriod = params.has("start") || params.has("end");
  /** Nothing narrowed the report, so an empty one means nothing has been
   *  recorded yet rather than that a filter excluded it. */
  const narrowed =
    !!search || !!repository || customPeriod || days !== 30 || !!scope.teamId;
  const queryParams = new URLSearchParams({
    days: String(days),
    dimension,
    sort,
    search,
    offset: String(offset),
    limit: String(pageSize),
  });
  for (const name of ["repository", "start", "end"]) {
    const value = params.get(name);
    if (value) queryParams.set(name, value);
  }
  const query = useQuery({
    queryKey: ["usage", queryParams.toString(), "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<UsageReport>(scope.path(`/api/usage?${queryParams}`), signal),
    staleTime: 60_000,
    refetchInterval: false,
  });
  const data = query.data;
  const totals = data?.totals;
  const grouping = dimensions.find((item) => item.value === dimension)!;
  useEffect(() => {
    document.title = "Review Agent · Usage";
  }, []);

  function groupLabel(row: UsageRow) {
    const href =
      dimension === "team" && row.team_id !== null
        ? `/usage?${new URLSearchParams({ dimension: "repository", team_id: String(row.team_id) })}`
        : dimension === "repository" && row.repository
          ? `/usage?${new URLSearchParams({ dimension: "requester", repository: row.repository })}`
          : null;
    return (
      <VStack gap={1}>
        {href ? (
          <Link to={href}>{row.label}</Link>
        ) : (
          <Text weight="medium">{row.label}</Text>
        )}
        <Text type="supporting">
          {dimension !== "repository" &&
            `${number.format(row.repository_count)} ${row.repository_count === 1 ? "repository" : "repositories"}`}
          {dimension === "team" && " · "}
          {dimension !== "requester" &&
            `${number.format(row.requester_count)} known GitHub ${row.requester_count === 1 ? "user" : "users"}`}
        </Text>
      </VStack>
    );
  }
  const columns: TableColumn<UsageRow>[] = [
    {
      key: "label",
      header:
        dimension === "team"
          ? "Team"
          : dimension === "repository"
            ? "Repository"
            : "GitHub user",
      width: proportional(2, { minWidth: 250 }),
      renderCell: groupLabel,
    },
    {
      key: "requests",
      header: "Requests",
      width: pixel(130),
      renderCell: (row) => (
        <VStack gap={1}>
          <Text weight="medium">{number.format(row.requests)}</Text>
          <Text type="supporting">
            {percent.format(row.requests / (totals?.requests || 1))} of matching
          </Text>
        </VStack>
      ),
    },
    {
      key: "total_tokens",
      header: "Recorded tokens",
      width: pixel(160),
      renderCell: (row) =>
        row.total_tokens === null ? (
          <Unknown />
        ) : (
          <Text>{number.format(row.total_tokens)}</Text>
        ),
    },
    {
      key: "reported_attempts",
      header: "Token reporting",
      width: pixel(165),
      renderCell: (row) => (
        <Text type="supporting">
          {row.started_attempts
            ? `${number.format(row.reported_attempts)} of ${number.format(row.started_attempts)} attempts`
            : "No attempts started"}
        </Text>
      ),
    },
    {
      key: "published_requests",
      header: "Published",
      width: pixel(105),
      renderCell: (row) => <Text>{number.format(row.published_requests)}</Text>,
    },
    {
      key: "failed_requests",
      header: "Failed",
      width: pixel(85),
      renderCell: (row) => <Text>{number.format(row.failed_requests)}</Text>,
    },
  ];

  return (
    <>
      <HStack gap={4} wrap="wrap" hAlign="between" vAlign="start">
        <VStack gap={3}>
          <Heading level={1}>Usage</Heading>
          <Prose>
            <Text as="p" color="secondary">
              Compare teams, repositories and the GitHub users who asked for
              reviews across {scope.team?.name ?? "all teams"}, and see where
              recorded tokens go. For how one deployment is performing —
              delivery times, failures and the daily rate — read{" "}
              <Link to="/overview">Statistics</Link>. The two count over
              different reporting rules, so their totals can differ.
            </Text>
          </Prose>
        </VStack>
        <Button
          label="Refresh"
          variant="ghost"
          onClick={() => void query.refetch()}
          isDisabled={query.isFetching}
        />
      </HStack>
      <HStack gap={4} wrap="wrap" vAlign="end">
        {customPeriod ? (
          <VStack gap={2}>
            <Text>Custom reporting period</Text>
            <Button
              label="Use last 30 days"
              variant="ghost"
              onClick={() => update({ start: "", end: "", days: "30" })}
            />
          </VStack>
        ) : (
          <Period days={days} change={(value) => update({ days: value })} />
        )}
        {scope.teamId && (
          <Button
            label="All teams"
            variant="ghost"
            onClick={() => update({ team_id: "", repository: "" })}
          />
        )}
        {repository && (
          <HStack gap={2} wrap="wrap" vAlign="center">
            <Text>{repository}</Text>
            <Button
              label="Clear repository"
              variant="ghost"
              onClick={() => update({ repository: "" })}
            />
            <Link to={`/history?${new URLSearchParams({ repository })}`}>
              View review requests
            </Link>
          </HStack>
        )}
      </HStack>
      <Freshness query={query} interval={false} />
      {data && (
        <Text type="supporting">
          Requests started {time(data.window_start)} to {time(data.window_end)}.
          Totals cover all matching rows.
        </Text>
      )}
      {(query.isPending || (totals && totals.requests > 0)) && (
        <Grid gap={6} columns={{ minWidth: 190, max: 4, repeat: "fit" }}>
          <Stat
            label="Review requests"
            value={totals?.requests}
            hint={
              totals &&
              `${number.format(totals.published_requests)} published · ${number.format(totals.failed_requests)} failed`
            }
          />
          <Stat
            label="Recorded tokens"
            value={totals?.total_tokens}
            hint={
              totals?.total_tokens != null &&
              `${number.format(totals.prompt_tokens ?? 0)} prompt · ${number.format(totals.completion_tokens ?? 0)} completion`
            }
          />
          <Stat
            label="Repositories using reviews"
            value={totals?.repository_count}
          />
          <Stat
            label="Known GitHub users"
            value={totals?.requester_count}
            hint={
              totals && totals.unknown_requester_requests > 0
                ? `${number.format(totals.unknown_requester_requests)} requests have no recorded user`
                : undefined
            }
          />
        </Grid>
      )}
      {totals && totals.requests > 0 && (
        <Prose>
          <Text as="p" type="supporting">
            {totals.started_attempts > 0
              ? `Tokens reported for ${number.format(totals.reported_attempts)} of ${number.format(totals.started_attempts)} started attempts (${percent.format(totals.reported_attempts / totals.started_attempts)}).`
              : "No worker attempts have started for these requests."}{" "}
            Recorded tokens include retries. Missing reports are excluded from
            token totals.
          </Text>
        </Prose>
      )}
      <TabList
        role="tablist"
        aria-label="Group usage by"
        value={dimension}
        onChange={(value) => update({ dimension: value, search: "" })}
        hasDivider
      >
        {dimensions.map((item) => (
          <Tab
            key={item.value}
            id={`usage-tab-${item.value}`}
            value={item.value}
            label={item.label}
            panelId={`usage-panel-${item.value}`}
          />
        ))}
      </TabList>
      {dimensions.map((item) => (
        <div
          key={item.value}
          id={`usage-panel-${item.value}`}
          role="tabpanel"
          aria-labelledby={`usage-tab-${item.value}`}
          hidden={dimension !== item.value}
          tabIndex={0}
        >
          {dimension === item.value && (
            <VStack gap={4}>
              <HStack gap={4} wrap="wrap" vAlign="end">
                <TextInput
                  label={`Find ${grouping.label.toLowerCase()}`}
                  value={draft}
                  onChange={setDraft}
                  onEnter={flush}
                  startIcon="search"
                  hasClear
                  width={250}
                  {...({
                    maxLength: 200,
                  } satisfies InputHTMLAttributes<HTMLInputElement>)}
                />
                <Selector
                  label="Rank by"
                  value={sort}
                  onChange={(value) => update({ sort: value })}
                  width={210}
                  options={[
                    { value: "requests", label: "Most requests" },
                    { value: "total_tokens", label: "Most recorded tokens" },
                    {
                      value: "published_requests",
                      label: "Most published reviews",
                    },
                  ]}
                />
              </HStack>
              {data && data.items.length > 0 ? (
                <>
                  <Table
                    aria-label={`${grouping.label} ranked by usage`}
                    data={data.items}
                    columns={columns}
                    idKey="key"
                    rowCount={data.totals.groups}
                    rowIndexStart={offset + 1}
                  />
                  <HStack gap={3} wrap="wrap" hAlign="between" vAlign="center">
                    <Text type="supporting">
                      {number.format(offset + 1)}–
                      {number.format(offset + data.items.length)} of{" "}
                      {number.format(data.totals.groups)}{" "}
                      {grouping.label.toLowerCase()}
                    </Text>
                    <HStack gap={2}>
                      <Button
                        label="Previous"
                        variant="ghost"
                        isDisabled={offset === 0 || query.isFetching}
                        onClick={() =>
                          update({
                            offset: String(Math.max(0, offset - pageSize)),
                          })
                        }
                      />
                      <Button
                        label="Next"
                        variant="ghost"
                        isDisabled={
                          !data.has_more ||
                          query.isFetching ||
                          offset + pageSize > 10000
                        }
                        onClick={() =>
                          update({ offset: String(offset + pageSize) })
                        }
                      />
                    </HStack>
                  </HStack>
                </>
              ) : (
                data && (
                  <Empty
                    title={
                      narrowed ? "No matching usage" : "Nothing recorded yet"
                    }
                  >
                    <VStack gap={3} hAlign="center">
                      <Text color="secondary">
                        {narrowed
                          ? "No retained requests match this scope, period and search."
                          : "Usage appears here once reviews have been asked for on GitHub."}
                      </Text>
                      {narrowed ? (
                        <Button
                          label="Reset filters"
                          variant="secondary"
                          onClick={() =>
                            update({
                              search: "",
                              repository: "",
                              offset: "",
                              start: "",
                              end: "",
                              days: "30",
                            })
                          }
                        />
                      ) : null}
                    </VStack>
                  </Empty>
                )
              )}
            </VStack>
          )}
        </div>
      ))}
      <Collapsible trigger="How usage is counted" defaultIsOpen={false}>
        <Prose>
          <VStack gap={3}>
            <Text as="p">
              Each accepted review request counts once. Worker retries add
              attempts and reported tokens to that request. Ignored triggers and
              duplicate deliveries do not create additional requests.
            </Text>
            <Text as="p">
              The period selects requests by their start time. Published and
              failed counts show their current outcome; tokens include every
              reported attempt for those requests, including reports received
              after the period ended.
            </Text>
            <Text as="p">
              Teams follow current repository ownership. Moving a repository
              moves its retained usage to its current team. GitHub users are
              grouped by the recorded login, ignoring letter case. Login changes
              may appear as separate users.
            </Text>
            <Text as="p">
              These figures use retained requests and provider-reported tokens.
              Deleted history and missing token reports are excluded.
            </Text>
          </VStack>
        </Prose>
      </Collapsible>
    </>
  );
}
