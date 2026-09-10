import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Section } from "@astryxdesign/core/Section";
import {
  Table,
  pixel,
  proportional,
  type TableColumn,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { InputHTMLAttributes } from "react";
import { Suspense, lazy, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { Account, RepositoryPage } from "./api";
import { APIError, read, write } from "./api";
import { AuditLog } from "./audit";
import { ScopedLink as Link, ScopeProvider, isAdmin, useScope } from "./scope";
import { RepositoryRequests, TeamDetail, TeamsPage } from "./teams";

import { Access, RepositoryTabs } from "./access";
import { Login, MyAccount, Users } from "./accounts";
import { ActivityPage } from "./activity";
import { ConsoleLayout } from "./console";
import { History, ReviewPage } from "./history";
import { OperationsPage } from "./operations";
import { OverviewPage } from "./overview";
import { FindingPage, QualityPage } from "./quality";
import { SettingsPage } from "./settings";
import {
  Empty,
  Freshness,
  Period,
  Prose,
  Stat,
  number,
  time,
  useFilters,
  useLiveSearch,
} from "./ui";

const ModelConnectionPage = lazy(() =>
  import("./modelConnections").then((module) => ({
    default: module.ModelConnectionPage,
  })),
);
const IntegrationsPage = lazy(() =>
  import("./integrations").then((module) => ({
    default: module.IntegrationsPage,
  })),
);
const ModelConnectionsPage = lazy(() =>
  import("./modelConnections").then((module) => ({
    default: module.ModelConnectionsPage,
  })),
);

function Repositories({ current }: { current: Account }) {
  const scope = useScope();
  const { params, days, update } = useFilters();
  const search = params.get("search") ?? "";
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const { draft, setDraft, flush } = useLiveSearch(search, (value) =>
    update({ search: value }, { replace: true }),
  );
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
    queryKey: ["repositories", queryParams.toString(), "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<RepositoryPage>(
        scope.path(`/api/repositories?${queryParams}`),
        signal,
      ),
  });
  const totals = query.data?.totals;
  function historyURL(repository: string, status = "all") {
    return `/history?${new URLSearchParams({ repository, days: String(days), status })}`;
  }
  // Width follows content, not header length: the repository name and its
  // supporting lines need the room, while each count is a short number. Left
  // to itself the table gave the widest content the narrowest column.
  const repositoryColumns: TableColumn<RepositoryPage["items"][number]>[] = [
    {
      key: "repository",
      header: "Repository",
      width: proportional(2, { minWidth: 260 }),
      renderCell: (repo) => (
        <VStack gap={1}>
          <Link to={historyURL(repo.repository)}>{repo.repository}</Link>
          <Text type="supporting">
            {repo.last_activity_at
              ? `Last activity ${time(repo.last_activity_at)}`
              : "No activity in this period"}
          </Text>
          <Text type="supporting">
            {repo.team_id ? (
              <Link to={`/teams/${repo.team_id}?team_id=${repo.team_id}`}>
                {repo.team_name}
              </Link>
            ) : isAdmin(current.role) ? (
              <Link
                to={`/teams?${new URLSearchParams({ assign_repository: String(repo.repository_id), repository_name: repo.repository })}`}
              >
                Assign to a team
              </Link>
            ) : (
              "Unassigned"
            )}
          </Text>
        </VStack>
      ),
    },
    {
      key: "prs_reviewed",
      header: "PRs reviewed",
      width: pixel(118),
      renderCell: (repo) => <Text>{number.format(repo.prs_reviewed)}</Text>,
    },
    {
      key: "published_requests",
      header: "Published",
      width: pixel(108),
      renderCell: (repo) => (
        <Link
          aria-label={`${repo.published_requests} published reviews for ${repo.repository}`}
          to={historyURL(repo.repository, "published")}
        >
          {number.format(repo.published_requests)}
        </Link>
      ),
    },
    {
      key: "failed_requests",
      header: "Failed",
      width: pixel(94),
      renderCell: (repo) => (
        <Link
          aria-label={`${repo.failed_requests} failed requests for ${repo.repository}`}
          to={historyURL(repo.repository, "failed")}
        >
          {number.format(repo.failed_requests)}
        </Link>
      ),
    },
    {
      key: "active_requests",
      header: "Active now",
      width: pixel(104),
      renderCell: (repo) => (
        <Link
          aria-label={`${repo.active_requests} active reviews for ${repo.repository}`}
          to={historyURL(repo.repository, "active")}
        >
          {number.format(repo.active_requests)}
        </Link>
      ),
    },
    {
      key: "latest_failed_prs",
      header: "Latest failures",
      width: pixel(150),
      renderCell: (repo) =>
        repo.latest_failed_prs ? (
          <Link to={historyURL(repo.repository, "latest_failed")}>
            {number.format(repo.latest_failed_prs)} PR
            {repo.latest_failed_prs === 1 ? "" : "s"} to check
          </Link>
        ) : (
          <Text color="secondary">None in period</Text>
        ),
    },
  ];
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Repositories &amp; access</Heading>
          <Text as="p">
            Review activity across your registered repositories.
          </Text>
        </VStack>
        <Link to={`/history?days=${days}`}>View all reviews</Link>
      </HStack>
      <RepositoryTabs role={current.role} />
      <HStack gap={4} wrap="wrap" align="end">
        <TextInput
          label={"Find a repository"}
          id="repo-search"
          startIcon="search"
          hasClear
          width={280}
          placeholder="owner/repository"
          value={draft}
          onChange={(value) => setDraft(value)}
          onEnter={flush}
          {...({
            maxLength: 200,
            list: "repository-name-options",
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />
        <datalist id="repository-name-options">
          {query.data?.items.map((repo) => (
            <option key={repo.repository} value={repo.repository} />
          ))}
        </datalist>
        <Period days={days} change={(value) => update({ days: value })} />
      </HStack>
      <Freshness query={query} />
      {/* With nothing matching, six zeroes and a sentence explaining what
          they would have counted say less than the empty state below. */}
      {query.data && totals && query.data.total > 0 && (
        <>
          <Grid gap={4} columns={{ minWidth: 160, max: 3, repeat: "fit" }}>
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
          </Grid>
          <Prose>
            <Text as="p" color="secondary">
              Totals cover all {query.data.total}{" "}
              {query.data.total === 1 ? "repository" : "repositories"}
              {search ? ` matching “${search}”` : ""} over the last {days} days.
              “Needs attention” counts PRs whose most recent request failed.
            </Text>
          </Prose>
        </>
      )}
      {query.data &&
        (query.data.items.length ? (
          <VStack gap={4}>
            <Text type="supporting" display="block">
              Requests started in the last {days} days. Active work is the
              current total.
            </Text>
            <Table
              aria-label="Repository statistics"
              data={query.data.items}
              columns={repositoryColumns}
              idKey="repository"
              density="balanced"
              dividers="rows"
              hasHover
              verticalAlign="top"
            />
          </VStack>
        ) : (
          <Empty
            title={search ? "No matching repositories" : "No repositories yet"}
          >
            {search ? (
              <VStack gap={3} hAlign="center">
                <Text color="secondary">
                  No repository matches “{search}”. Try another name.
                </Text>
                <Button
                  label={"Clear the search"}
                  variant="secondary"
                  onClick={() => update({ search: "" })}
                />
              </VStack>
            ) : (
              "Repositories appear after they have been registered by Review Agent. Request a review on GitHub to create review activity."
            )}
          </Empty>
        ))}
      {query.data && (offset > 0 || query.data.has_more) && (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"Previous"}
            variant="secondary"
            type="submit"

            isDisabled={offset === 0}
            onClick={() => update({ offset: String(Math.max(0, offset - 50)) })}
          />
          <Text>Page {Math.floor(offset / 50) + 1}</Text>
          <Button
            label={"Next"}
            variant="secondary"
            type="submit"

            isDisabled={!query.data.has_more || offset >= 10000}
            onClick={() => update({ offset: String(offset + 50) })}
          />
        </HStack>
      )}
      <Collapsible
        defaultIsOpen={false}
        trigger={
          <HStack gap={3} wrap="wrap" vAlign="center">
            How these counts work
          </HStack>
        }
      >
        <VStack gap={4}>
          <Text as="p">
            <strong>PRs reviewed</strong> counts distinct PRs with a published
            review from a request started in this period.{" "}
            <strong>Published reviews</strong> counts those requests, including
            repeat reviews. <strong>Failed requests</strong> includes failures
            later recovered by another request. <strong>Latest failures</strong>{" "}
            counts PRs whose most recent request failed in this period.
          </Text>
          <Text as="p">
            Publication means a result reached GitHub. It does not mean the PR
            is approved, defect-free, or fully covered. These statistics reflect
            retained Review Agent records.
          </Text>
        </VStack>
      </Collapsible>
    </>
  );
}

function initialTheme(): "light" | "dark" {
  try {
    const saved = localStorage.getItem("review-agent.theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    // Theme switching also works when browser storage is unavailable.
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function App() {
  const [theme, setTheme] = useState(initialTheme);
  useEffect(() => {
    try {
      localStorage.setItem("review-agent.theme", theme);
    } catch {
      // Keep the choice for the current tab when storage is unavailable.
    }
  }, [theme]);
  return (
    <Theme theme={neutralTheme} mode={theme}>
      <Application
        theme={theme}
        toggleTheme={() =>
          setTheme((value) => (value === "light" ? "dark" : "light"))
        }
      />
    </Theme>
  );
}

function Application({
  theme,
  toggleTheme,
}: {
  theme: "light" | "dark";
  toggleTheme: () => void;
}) {
  const isNarrow = useMediaQuery("(max-width: 768px)");
  const client = useQueryClient();
  const { pathname, search: routeSearch } = useLocation();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: ({ signal }) => read<Account>("/api/me", signal),
    retry: false,
  });
  const signedOut = me.error instanceof APIError && me.error.status === 401;
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
      <main>
        <Center minHeight="100dvh" padding={6}>
          <VStack gap={4} width="100%" maxWidth={400}>
            <Heading level={1}>Review Agent</Heading>
            <Freshness query={me} />
          </VStack>
        </Center>
      </main>
    );
  const current = me.data;
  return (
    <ScopeProvider current={current}>
      <ConsoleLayout
        current={current}
        theme={theme}
        toggleTheme={toggleTheme}
        logout={() => logout.mutate()}
        signingOut={logout.isPending}
      >
        <Section padding={0} maxWidth={1440}>
          <VStack gap={6} padding={6} paddingInline={isNarrow ? 4 : 6}>
            {logout.isError && (
              <Text as="p" role="alert">
                Could not sign out. Please try again.
              </Text>
            )}
            <Routes>
              <Route path="/teams" element={<TeamsPage />} />
              <Route path="/teams/:teamId" element={<TeamDetail />} />
              <Route
                path="/repository-requests"
                element={<RepositoryRequests />}
              />
              <Route
                path="/audit"
                element={
                  isAdmin(current.role) ? (
                    <AuditLog />
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route path="/" element={<ActivityPage />} />
              <Route path="/overview" element={<OverviewPage />} />
              <Route
                path="/repositories"
                element={<Repositories current={current} />}
              />
              <Route path="/history" element={<History />} />
              <Route path="/history/:runId" element={<ReviewPage />} />
              <Route
                path="/model-connections"
                element={
                  <Suspense
                    fallback={
                      <Text as="p" role="status">
                        Loading model connections…
                      </Text>
                    }
                  >
                    <ModelConnectionsPage />
                  </Suspense>
                }
              />
              <Route
                path="/model-connections/:connectionId"
                element={
                  <Suspense
                    fallback={
                      <Text as="p" role="status">
                        Loading connection…
                      </Text>
                    }
                  >
                    <ModelConnectionPage />
                  </Suspense>
                }
              />
              <Route path="/quality" element={<QualityPage />} />
              <Route
                path="/findings/:fingerprint"
                element={<FindingPage key={`${pathname}${routeSearch}`} />}
              />
              <Route
                path="/settings"
                element={
                  current.role === "owner" ? (
                    <SettingsPage />
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route
                path="/access"
                element={
                  isAdmin(current.role) ? (
                    <Access />
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route
                path="/operations"
                element={
                  isAdmin(current.role) ? (
                    <OperationsPage />
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route
                path="/users"
                element={
                  isAdmin(current.role) ? (
                    <Users current={current} />
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route
                path="/integrations"
                element={
                  isAdmin(current.role) ? (
                    <Suspense
                      fallback={<Text as="p">Loading integrations…</Text>}
                    >
                      <IntegrationsPage />
                    </Suspense>
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route
                path="/account"
                element={<MyAccount current={current} />}
              />
            </Routes>
          </VStack>
        </Section>
      </ConsoleLayout>
    </ScopeProvider>
  );
}
