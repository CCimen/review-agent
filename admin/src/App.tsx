import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Section } from "@astryxdesign/core/Section";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { FormEvent, InputHTMLAttributes } from "react";
import { Suspense, lazy, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { Account, RepositoryPage } from "./api";
import { APIError, read, write } from "./api";
import { AuditLog } from "./audit";
import { ScopedLink as Link, ScopeProvider, isAdmin, useScope } from "./scope";
import { RepositoryRequests, TeamDetail, TeamsPage } from "./teams";
import { Form } from "./ui";

import { Access, RepositoryTabs } from "./access";
import { Login, MyAccount, Users } from "./accounts";
import { ActivityPage } from "./activity";
import { ConsoleLayout } from "./console";
import { History, ReviewPage } from "./history";
import { OperationsPage } from "./operations";
import { OverviewPage } from "./overview";
import { FindingPage, QualityPage } from "./quality";
import { SettingsPage } from "./settings";
import { Empty, Freshness, Period, Stat, number, time, useFilters } from "./ui";

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
    queryKey: ["repositories", queryParams.toString(), "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<RepositoryPage>(
        scope.path(`/api/repositories?${queryParams}`),
        signal,
      ),
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
      <Form onSubmit={submit}>
        <HStack gap={3} wrap="wrap" vAlign="end">
          <TextInput
            label={"Find a repository"}
            id="repo-search"
            placeholder="Search owner or repository"
            value={draft}
            onChange={(value) => setDraft(value)}
            {...({
              maxLength: 200,
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
          />

          <Period days={days} change={(value) => update({ days: value })} />
          <Button label="Search" type="submit" />
        </HStack>
      </Form>
      <Freshness query={query} />
      {query.data && totals && (
        <>
          <Grid gap={4} columns={{ minWidth: 160, max: 6, repeat: "fit" }}>
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
          <Text as="p" color="secondary">
            Totals cover all {query.data.total}{" "}
            {query.data.total === 1 ? "repository" : "repositories"}
            {search ? ` matching “${search}”` : ""} over the last {days} days.
            “Needs attention” counts PRs whose most recent request failed.
          </Text>
        </>
      )}
      {query.data &&
        (query.data.items.length ? (
          <VStack gap={4}>
            <VStack
              gap={0}

              tabIndex={0}
              role="region"
              aria-label="Repository statistics"
            >
              {/* Keep table semantics when the rows reflow on small screens. */}
              <Table role="table">
                <caption>
                  <Text type="supporting" display="block" justify="start">
                    Requests started in the last {days} days. Active work is the
                    current total.
                  </Text>
                </caption>
                <TableHeader role="rowgroup">
                  <TableRow role="row">
                    <TableHeaderCell scope="col" role="columnheader">
                      Repository
                    </TableHeaderCell>
                    <TableHeaderCell scope="col" role="columnheader">
                      PRs reviewed
                    </TableHeaderCell>
                    <TableHeaderCell scope="col" role="columnheader">
                      Published reviews
                    </TableHeaderCell>
                    <TableHeaderCell scope="col" role="columnheader">
                      Failed requests
                    </TableHeaderCell>
                    <TableHeaderCell scope="col" role="columnheader">
                      Active now
                    </TableHeaderCell>
                    <TableHeaderCell scope="col" role="columnheader">
                      Latest failures
                    </TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody role="rowgroup">
                  {query.data.items.map((repo) => (
                    <TableRow key={repo.repository} role="row">
                      <TableHeaderCell scope="row" role="rowheader">
                        <Link to={historyURL(repo.repository)}>
                          {repo.repository}
                        </Link>
                        <Text
                          color="secondary"
                          display="block"
                          type="supporting"
                        >
                          {repo.last_activity_at
                            ? `Last activity ${time(repo.last_activity_at)}`
                            : "No activity in this period"}
                        </Text>
                        <Text
                          color="secondary"
                          display="block"
                          type="supporting"
                        >
                          {repo.team_id ? (
                            <Link
                              to={`/teams/${repo.team_id}?team_id=${repo.team_id}`}
                            >
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
                      </TableHeaderCell>
                      <TableCell role="cell">
                        <Text>{number.format(repo.prs_reviewed)}</Text>
                      </TableCell>
                      <TableCell role="cell">
                        <Link
                          aria-label={`${repo.published_requests} published reviews for ${repo.repository}`}
                          to={historyURL(repo.repository, "published")}
                        >
                          {number.format(repo.published_requests)}
                        </Link>
                      </TableCell>
                      <TableCell role="cell">
                        <Link
                          aria-label={`${repo.failed_requests} failed requests for ${repo.repository}`}
                          to={historyURL(repo.repository, "failed")}
                        >
                          {number.format(repo.failed_requests)}
                        </Link>
                      </TableCell>
                      <TableCell role="cell">
                        <Link
                          aria-label={`${repo.active_requests} active reviews for ${repo.repository}`}
                          to={historyURL(repo.repository, "active")}
                        >
                          {number.format(repo.active_requests)}
                        </Link>
                      </TableCell>
                      <TableCell role="cell">
                        {repo.latest_failed_prs ? (
                          <Link
                            to={historyURL(repo.repository, "latest_failed")}
                          >
                            {number.format(repo.latest_failed_prs)} PR
                            {repo.latest_failed_prs === 1 ? "" : "s"} to check
                          </Link>
                        ) : (
                          <Text color="secondary">None in period</Text>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </VStack>
          </VStack>
        ) : (
          <Empty
            title={search ? "No matching repositories" : "No repositories yet"}
          >
            {search ? (
              <>
                No repository matches “{search}”. Try another name, or{" "}
                <Button
                  label={"clear the search"}
                  variant="primary"
                  type="submit"

                  onClick={() => update({ search: "" })}
                />
                .
              </>
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
        <Section padding={6} paddingInline={isNarrow ? 4 : 6} maxWidth={1440}>
          <VStack gap={6}>
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
