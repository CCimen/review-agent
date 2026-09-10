import { Button } from "@astryxdesign/core/Button";
import { Center } from "@astryxdesign/core/Center";
import { ComplexSelector } from "@astryxdesign/core/ComplexSelector";
import { VStack } from "@astryxdesign/core/Layout";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
import { List, ListItem } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { UseQueryResult } from "@tanstack/react-query";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ComponentProps, InputHTMLAttributes, ReactNode } from "react";
import { createContext, useContext, useEffect, useState } from "react";
import type { LinkProps, To } from "react-router-dom";
import {
  Link,
  createPath,
  parsePath,
  useLocation,
  useNavigate,
} from "react-router-dom";
import type { Account, Team, TeamPage } from "./api";
import { APIError, read } from "./api";
import { Empty, Freshness } from "./ui";

export const roleLabels: Record<Account["role"], string> = {
  owner: "Owner",
  admin: "Admin",
  viewer: "Global viewer",
  member: "Member",
};
export const isAdmin = (role: Account["role"]) =>
  role === "owner" || role === "admin";

interface Scope {
  current: Account;
  teamId: string | null;
  team: Team | undefined;
  teamQuery: UseQueryResult<Team>;
  teams: TeamPage | undefined;
  key: string;
  path: (path: string) => string;
}
const ScopeContext = createContext<Scope | null>(null);
export function useScope() {
  const scope = useContext(ScopeContext);
  if (!scope) throw new Error("Console scope is required");
  return scope;
}

/** Preserve the viewing context across console pages, while keeping page filters local. */
export function contextualTo(to: To, search: string): To {
  if (typeof to === "string" && /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(to))
    return to;
  const target = typeof to === "string" ? parsePath(to) : to;
  const params = new URLSearchParams(target.search);
  const current = new URLSearchParams(search);
  for (const name of ["team_id", "days", "start", "end"]) {
    const value = current.get(name);
    if (value && !params.has(name)) params.set(name, value);
  }
  return { ...target, search: params.size ? `?${params}` : "" };
}
export function ScopedLink({
  to,
  ...props
}: Omit<LinkProps, "color" | "type">) {
  const { children, ...linkProps } = props;
  return (
    <AstryxLink
      as={ScopedAnchor}
      href={typeof to === "string" ? to : createPath(to)}
      {...linkProps}
    >
      {children}
    </AstryxLink>
  );
}
/** Adapt Astryx's anchor contract to the router while retaining viewing context. */
export function ScopedAnchor({ href = "/", ...props }: ComponentProps<"a">) {
  const { search } = useLocation();
  // Astryx supplies a `to` alongside `href`; the scoped destination owns it.
  return <Link {...props} to={contextualTo(href, search)} />;
}

export function ScopeProvider({
  current,
  children,
}: {
  current: Account;
  children: ReactNode;
}) {
  const { pathname, search } = useLocation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const requestedTeamId = new URLSearchParams(search).get("team_id");
  const routeTeamId = /^\/teams\/([1-9]\d{0,18})$/.exec(pathname)?.[1] ?? null;
  const identity = `${current.id}:${current.role}:${current.access_revision}`;
  const teams = useQuery({
    queryKey: ["teams", "selector", identity],
    queryFn: ({ signal }) => read<TeamPage>("/api/teams?limit=100", signal),
  });
  const onlyTeam =
    current.role === "member" && teams.data?.total === 1
      ? teams.data.items[0]
      : undefined;
  const teamId =
    routeTeamId ?? requestedTeamId ?? (onlyTeam ? String(onlyTeam.id) : null);
  const valid = teamId === null || /^[1-9]\d{0,18}$/.test(teamId);
  const selected = useQuery({
    queryKey: ["team", teamId, identity],
    queryFn: ({ signal }) => read<Team>(`/api/teams/${teamId}`, signal),
    enabled: valid && teamId !== null,
    retry: false,
  });
  const denied =
    !valid ||
    (selected.error instanceof APIError &&
      [401, 403, 404, 422].includes(selected.error.status));
  const key = `${identity}:${teamId ?? "all"}:${selected.data?.role ?? ""}`;
  useEffect(() => {
    // Cancel requests as well as discarding results after an access revision changes.
    const predicate = (query: { queryKey: readonly unknown[] }) =>
      query.queryKey.includes("scoped") && !query.queryKey.includes(key);
    void client
      .cancelQueries({ predicate })
      .then(() => client.removeQueries({ predicate }));
  }, [client, key]);
  useEffect(() => {
    if (!denied) return;
    const predicate = (query: { queryKey: readonly unknown[] }) =>
      query.queryKey.includes("scoped");
    void client
      .cancelQueries({ predicate })
      .then(() => client.removeQueries({ predicate }));
  }, [denied, client]);
  useEffect(() => {
    const destination =
      routeTeamId ??
      (requestedTeamId === null && onlyTeam ? String(onlyTeam.id) : null);
    if (destination === null || destination === requestedTeamId) return;
    const params = new URLSearchParams(search);
    params.set("team_id", destination);
    navigate({ pathname, search: params.toString() }, { replace: true });
  }, [pathname, routeTeamId, requestedTeamId, onlyTeam, search, navigate]);
  const path = (url: string) => {
    const target = new URL(url, "https://console.invalid");
    if (teamId !== null) target.searchParams.set("team_id", teamId);
    return `${target.pathname}${target.search}`;
  };
  const globalPage = [
    "/teams",
    "/repository-requests",
    "/audit",
    "/users",
    "/account",
    "/settings",
    "/operations",
    "/access",
  ].some((route) => pathname === route || pathname.startsWith(`${route}/`));
  return (
    <ScopeContext
      value={{
        current,
        teamId,
        team: selected.data,
        teamQuery: selected,
        teams: teams.data,
        key,
        path,
      }}
    >
      {denied && !globalPage ? (
        <main>
          <Center minHeight="100dvh" padding={6}>
            <Empty title="Team access is unavailable">
              This team is no longer available to your account.{" "}
              <Link to="/teams">Choose an available team</Link>.
            </Empty>
          </Center>
        </main>
      ) : teamId && !selected.data && !globalPage ? (
        <main>
          <Center minHeight="100dvh" padding={6}>
            <Freshness query={selected} />
          </Center>
        </main>
      ) : current.role === "member" && !globalPage && !teams.data ? (
        <main>
          <Center minHeight="100dvh" padding={6}>
            <Freshness query={teams} />
          </Center>
        </main>
      ) : current.role === "member" &&
        !globalPage &&
        teams.data?.total === 0 ? (
        <main>
          <Center minHeight="100dvh" padding={6}>
            <Empty title="Your account is ready">
              Ask a platform administrator or team maintainer to add you to a
              team. <Link to="/account">Your account</Link>
            </Empty>
          </Center>
        </main>
      ) : (
        <div key={key}>{children}</div>
      )}
    </ScopeContext>
  );
}

export function ScopeSelector() {
  const scope = useScope();
  const { teamId, team, teams, current } = scope;
  const { search } = useLocation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [term, setTerm] = useState("");
  const [after, setAfter] = useState(0);
  useEffect(() => {
    const timer = window.setTimeout(() => setTerm(draft.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [draft]);
  const matches = useQuery({
    queryKey: ["teams", "picker", term, after, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPage>(
        `/api/teams?${new URLSearchParams({ search: term, after_id: String(after), limit: "20" })}`,
        signal,
      ),
    enabled: open && (term.length > 0 || after > 0),
  });
  const page = term || after ? matches.data : teams;
  const label = teamId
    ? (team?.name ?? "Team unavailable")
    : current.role === "member"
      ? "All my teams"
      : "All teams";
  const select = (id: string) => {
    const params = new URLSearchParams();
    const currentParams = new URLSearchParams(search);
    for (const name of ["days", "start", "end"]) {
      const value = currentParams.get(name);
      if (value) params.set(name, value);
    }
    if (id) params.set("team_id", id);
    navigate({ pathname: "/", search: params.toString() });
  };
  if (current.role === "member" && (!teams || teams.total <= 1)) {
    const onlyTeam = teams?.items[0];
    return onlyTeam ? (
      <VStack gap={1}>
        <Text type="supporting">Team workspace</Text>
        <Text weight="medium">{onlyTeam.name}</Text>
      </VStack>
    ) : null;
  }
  return (
    <ComplexSelector
      label="Switch team"
      value={teamId ?? ""}
      triggerLabel={label}
      width="100%"
      onChange={select}
      onOpenChange={(isOpen) => {
        setOpen(isOpen);
        if (isOpen) {
          setDraft("");
          setTerm("");
          setAfter(0);
        }
      }}
    >
      {(value, change, close) => (
        <VStack gap={3} width="min(320px, calc(100dvw - var(--spacing-8)))">
          <TextInput
            label={"Find a team"}
            id="team-picker-search"
            placeholder="Search teams…"
            value={draft}
            hasAutoFocus={true}
            onChange={(value) => {
              setDraft(value);
              setAfter(0);
            }}
            {...({
              maxLength: 80,
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
          />

          {(term || after) && matches.isFetching ? (
            <Text role="status">Finding teams…</Text>
          ) : null}
          {(term || after) && matches.isError ? (
            <Freshness query={matches} />
          ) : null}
          <List aria-label="Available teams" density="compact">
            <ListItem
              label={current.role === "member" ? "All my teams" : "All teams"}
              description={
                current.role === "member"
                  ? "Combined team activity"
                  : "Platform view"
              }
              isSelected={value === ""}
              onClick={() => {
                change("");
                close();
              }}
            />
            {page?.items.map((item) => (
              <ListItem
                key={item.id}
                label={item.name}
                description={`${item.repository_count} repositories${item.role ? ` · ${item.role}` : ""}`}
                isSelected={String(item.id) === value}
                onClick={() => {
                  change(String(item.id));
                  close();
                }}
              />
            ))}
          </List>
          {page?.items.length === 0 ? (
            // An empty deployment is not a failed search. Saying "no matches"
            // when nothing was typed blames the reader for the empty list.
            <Text>
              {draft.trim()
                ? "No teams match this search."
                : "No teams yet. Create one from team management."}
            </Text>
          ) : null}
          {page?.next_after_id ? (
            <Button
              label="More teams"
              variant="ghost"
              onClick={() => setAfter(page.next_after_id ?? 0)}
            />
          ) : null}
          <AstryxLink as={ScopedAnchor} href="/teams" onClick={close}>
            {isAdmin(current.role) ? "Manage teams" : "Browse teams"}
          </AstryxLink>
        </VStack>
      )}
    </ComplexSelector>
  );
}
