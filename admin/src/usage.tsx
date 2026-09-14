import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { ProgressBar } from "@astryxdesign/core/ProgressBar";
import { Selector } from "@astryxdesign/core/Selector";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Token } from "@astryxdesign/core/Token";
import {
  Table,
  pixel,
  proportional,
  type TableColumn,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useEffect, type InputHTMLAttributes } from "react";
import { Navigate } from "react-router-dom";
import type { UsageReport, UsageRow } from "./api";
import { read } from "./api";
import { ReviewPurposeFilter, selectedReviewPurpose } from "./reviewPurpose";
import { ScopedLink as Link, isAdmin, useScope } from "./scope";
import {
  Empty,
  Freshness,
  Loading,
  Period,
  Prose,
  Stat,
  Unknown,
  number,
  time,
  useFilters,
  useLiveSearch,
} from "./ui";

/** `noun` is the label as it reads inside a sentence; lowercasing the label
 *  turned "GitHub" into "github". */
const dimensions: {
  value: UsageReport["dimension"];
  label: string;
  noun: string;
}[] = [
  { value: "team", label: "Teams", noun: "teams" },
  { value: "repository", label: "Repositories", noun: "repositories" },
  { value: "requester", label: "GitHub users", noun: "GitHub users" },
];
const percent = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumFractionDigits: 1,
});
const pageSize = 25;

/** Where a group's usage leads to: a team to its repositories, a repository
 *  to the people who asked. A GitHub login has no request list to open, so
 *  it stays text. */
function drillHref(dimension: UsageReport["dimension"], row: UsageRow) {
  return dimension === "team" && row.team_id !== null
    ? `/usage?${new URLSearchParams({ dimension: "repository", team_id: String(row.team_id) })}`
    : dimension === "repository" && row.repository
      ? `/usage?${new URLSearchParams({ dimension: "requester", repository: row.repository })}`
      : /* A person opens onto the repositories they asked about, which is the
           follow-up an administrator is usually after; the grouping stays a
           breakdown rather than becoming a separate profile page. */
        dimension === "requester" && row.requester
        ? `/usage?${new URLSearchParams({ dimension: "repository", requester: row.requester })}`
        : null;
}

/** The follow-up view at a glance: who asked for the most reviews, by team,
 *  repository and GitHub user, ranked against the leader so the bars read as
 *  a comparison rather than as shares of a total that is printed beside them.
 *  Each panel is the same report the tab below pages through, cut to five. */
function MostActive({
  base,
  scopeKey,
  path,
  selected,
  hrefFor,
}: {
  base: URLSearchParams;
  scopeKey: string;
  path: (url: string) => string;
  selected: UsageReport["dimension"];
  hrefFor: (dimension: UsageReport["dimension"]) => string;
}) {
  return (
    <Grid gap={5} columns={{ minWidth: 260, max: 3, repeat: "fit" }}>
      {dimensions.map((item) => (
        <MostActivePanel
          key={item.value}
          dimension={item.value}
          noun={item.noun}
          base={base}
          scopeKey={scopeKey}
          path={path}
          isSelected={selected === item.value}
          href={hrefFor(item.value)}
        />
      ))}
    </Grid>
  );
}

function MostActivePanel({
  dimension,
  noun,
  base,
  scopeKey,
  path,
  isSelected,
  href,
}: {
  dimension: UsageReport["dimension"];
  noun: string;
  base: URLSearchParams;
  scopeKey: string;
  path: (url: string) => string;
  isSelected: boolean;
  href: string;
}) {
  const params = new URLSearchParams(base);
  params.set("dimension", dimension);
  params.set("sort", "requests");
  params.set("limit", "5");
  params.set("offset", "0");
  params.delete("search");
  const query = useQuery({
    queryKey: ["usage", params.toString(), "scoped", scopeKey],
    queryFn: ({ signal }) =>
      read<UsageReport>(path(`/api/usage?${params}`), signal),
    staleTime: 60_000,
    refetchInterval: false,
  });
  const rows = query.data?.items ?? [];
  const lead = rows[0]?.requests ?? 0;
  const total = query.data?.totals.requests ?? 0;
  return (
    <Card padding={5}>
      <VStack gap={4}>
        <VStack gap={1}>
          <Heading level={3}>Most active {noun}</Heading>
          <Text type="supporting">
            {query.data
              ? `${number.format(query.data.totals.groups)} with requests in the period`
              : "By review requests"}
          </Text>
        </VStack>
        {query.isPending ? (
          <Loading rows={5} label={`Loading most active ${noun}`} />
        ) : query.isError ? (
          <Text color="secondary">Unavailable right now.</Text>
        ) : rows.length === 0 ? (
          <Text color="secondary">No requests in this period.</Text>
        ) : (
          <VStack gap={3}>
            {rows.map((row) => {
              const href = drillHref(dimension, row);
              return (
                <VStack key={row.key} gap={1} className="console-rank">
                  <HStack gap={3} hAlign="between" vAlign="center">
                    {href ? (
                      <Link to={href}>{row.label}</Link>
                    ) : (
                      <Text>{row.label}</Text>
                    )}
                    <Text hasTabularNumbers>
                      {number.format(row.requests)}
                      <Text type="supporting">
                        {" "}
                        · {percent.format(row.requests / (total || 1))}
                      </Text>
                    </Text>
                  </HStack>
                  {/* Ranked against the leader. theme.css gives the fill the
                      console's accent: the library's own accent is the blue
                      this deployment spends on work that is running, and the
                      grey it falls back to read as a disabled control rather
                      than as activity that was actually recorded. */}
                  <ProgressBar
                    label={`${row.label}: ${number.format(row.requests)} review requests`}
                    isLabelHidden
                    value={row.requests}
                    max={lead || 1}
                    variant="neutral"
                  />
                </VStack>
              );
            })}
          </VStack>
        )}
        {/* The three cards and the three tabs below name the same groupings,
            so each card says whether it is the one listed in full and offers
            to switch if it is not. */}
        {query.data && query.data.totals.groups > 0 ? (
          <HStack hAlign="start">
            {isSelected ? (
              <Text type="supporting">Listed in full below</Text>
            ) : (
              /* A link, not a button: selecting a grouping changes the
                 address, so it can be opened in a new tab and shared, and it
                 sits flush with the rows above it. */
              <Link to={href}>See all {noun}</Link>
            )}
          </HStack>
        ) : null}
      </VStack>
    </Card>
  );
}

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
  const purpose = selectedReviewPurpose(params.get("purpose"));
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
  const requester = params.get("requester") ?? "";
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
    !!purpose ||
    !!search ||
    !!repository ||
    !!requester ||
    customPeriod ||
    days !== 30 ||
    !!scope.teamId;
  const queryParams = new URLSearchParams({
    days: String(days),
    dimension,
    sort,
    search,
    offset: String(offset),
    limit: String(pageSize),
  });
  if (purpose) queryParams.set("purpose", purpose);
  for (const name of ["repository", "requester", "start", "end"]) {
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
  /** Selecting a grouping is a change of address, so the cards can link to it
   *  instead of reaching back into this component's state. */
  const dimensionHref = (value: UsageReport["dimension"]) => {
    const next = new URLSearchParams(params);
    next.set("dimension", value);
    next.delete("search");
    next.delete("offset");
    return `/usage?${next}`;
  };
  useEffect(() => {
    document.title = "Review Agent · Usage";
  }, []);

  function groupLabel(row: UsageRow) {
    const href = drillHref(dimension, row);
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
      width: pixel(150),
      renderCell: (row) => (
        <VStack gap={1} className="console-rank">
          <Text weight="medium" hasTabularNumbers>
            {number.format(row.requests)}
          </Text>
          <ProgressBar
            label={`${row.label}: ${percent.format(row.requests / (totals?.requests || 1))} of matching requests`}
            isLabelHidden
            value={row.requests}
            max={totals?.requests || 1}
            variant="neutral"
          />
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
      </HStack>
      <HStack gap={4} wrap="wrap" vAlign="end">
        <ReviewPurposeFilter
          value={purpose}
          onChange={(value) => update({ purpose: value })}
        />
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
          /* A filter that is on reads as a token you can take off, under a
             label like the two selectors beside it, rather than as a bare
             repository name followed by two competing actions. */
          <VStack gap={2}>
            <Text>Repository</Text>
            <HStack gap={3} wrap="wrap" vAlign="center">
              <Token
                label={repository}
                onRemove={() => update({ repository: "" })}
                description="Active repository filter. Remove to cover all repositories."
              />
              <Link to={`/history?${new URLSearchParams({ repository })}`}>
                View review requests
              </Link>
            </HStack>
          </VStack>
        )}
        {requester && (
          <VStack gap={2}>
            <Text>GitHub user</Text>
            <HStack gap={3} wrap="wrap" vAlign="center">
              <Token
                label={requester}
                onRemove={() => update({ requester: "" })}
                description="Active GitHub user filter. Every grouping covers only the reviews this person asked for."
              />
            </HStack>
          </VStack>
        )}
        {/* Every drill changes the address, so the browser's own Back button
            returns to the previous view and each view can be linked. What the
            page owes the reader is a visible list of what is filtering it, and
            one way out of all of it at once — the same control Activity has. */}
        {(purpose || repository || requester || search) && (
          <Button
            label="Clear filters"
            variant="ghost"
            onClick={() =>
              update({
                purpose: "",
                repository: "",
                requester: "",
                search: "",
              })
            }
          />
        )}
      </HStack>
      {/* The control that fetches again belongs beside the line that says
          how old the figures are, not at the far corner of a 1440px header. */}
      <HStack gap={3} wrap="wrap" vAlign="center">
        <Freshness query={query} interval={false} />
        <Button
          label={query.isFetching ? "Refreshing…" : "Refresh"}
          icon={<RefreshCw size="1em" aria-hidden="true" />}
          variant="ghost"
          size="sm"
          onClick={() => void query.refetch()}
          isDisabled={query.isFetching}
        />
      </HStack>
      {data && (
        <Text type="supporting">
          Requests started {time(data.window_start)} to {time(data.window_end)}
          {requester ? ` that ${requester} asked for` : ""}. Totals cover all
          matching rows.
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
      {totals && totals.requests > 0 ? (
        <MostActive
          base={queryParams}
          scopeKey={scope.key}
          path={(url) => scope.path(url)}
          selected={dimension}
          hrefFor={dimensionHref}
        />
      ) : null}
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
                  label={`Find ${grouping.noun}`}
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
                      {number.format(data.totals.groups)} {grouping.noun}
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
