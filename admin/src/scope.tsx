import {
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import {
  Link,
  NavLink,
  parsePath,
  useLocation,
  useNavigate,
} from "react-router-dom";
import type { LinkProps, NavLinkProps, To } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { UseQueryResult } from "@tanstack/react-query";
import { APIError, read } from "./api";
import type { Account, Team, TeamPage } from "./api";
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
export function ScopedLink({ to, ...props }: LinkProps) {
  const { search } = useLocation();
  return <Link {...props} to={contextualTo(to, search)} />;
}
export function ScopedNavLink({ to, ...props }: NavLinkProps) {
  const { search } = useLocation();
  return <NavLink {...props} to={contextualTo(to, search)} />;
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
  const teamId = routeTeamId ?? requestedTeamId ?? (onlyTeam ? String(onlyTeam.id) : null);
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
    const destination = routeTeamId ?? (requestedTeamId === null && onlyTeam ? String(onlyTeam.id) : null);
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
        <main className="scope-unavailable">
          <Empty title="Team access is unavailable">
            This team is no longer available to your account.{" "}
            <Link to="/teams">Choose an available team</Link>.
          </Empty>
        </main>
      ) : teamId && !selected.data && !globalPage ? (
        <main>
          <Freshness query={selected} />
        </main>
      ) : current.role === "member" && !globalPage && !teams.data ? (
        <main>
          <Freshness query={teams} />
        </main>
      ) : current.role === "member" &&
        !globalPage &&
        teams.data?.total === 0 ? (
        <main>
          <Empty title="Your account is ready">
            Ask a platform administrator or team maintainer to add you to a
            team. <Link to="/account">Your account</Link>
          </Empty>
        </main>
      ) : (
        <div key={key} className="scope-content">
          {children}
        </div>
      )}
    </ScopeContext>
  );
}

export function ScopeSelector() {
  const scope = useScope();
  const { teamId, team, teams, current } = scope;
  const { search } = useLocation();
  const navigate = useNavigate();
  const popoverId = useId();
  const popover = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [term, setTerm] = useState("");
  const [after, setAfter] = useState(0);
  const [position, setPosition] = useState({ top: 0, left: 0 });
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
    popover.current?.hidePopover();
    navigate({ pathname: "/", search: params.toString() });
  };
  if (current.role === "member" && (!teams || teams.total <= 1)) {
    const onlyTeam = teams?.items[0];
    return onlyTeam ? (
      <div className="scope-selector">
        <div className="scope-current">
          <span className="team-monogram" aria-hidden="true">
            {onlyTeam.name.slice(0, 1).toUpperCase()}
          </span>
          <span className="scope-name">
            {onlyTeam.name}
            <small>Team workspace</small>
          </span>
        </div>
      </div>
    ) : null;
  }
  return (
    <div className="scope-selector">
      <button
        className="scope-trigger secondary"
        popoverTarget={popoverId}
        aria-expanded={open}
        aria-label={`Switch team. Current view: ${label}`}
        onClick={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          setPosition({
            top: Math.min(
              bounds.bottom + 6,
              Math.max(8, window.innerHeight - 440),
            ),
            left: Math.max(8, Math.min(bounds.left, window.innerWidth - 352)),
          });
        }}
      >
        <span className="team-monogram" aria-hidden="true">
          {teamId ? (team?.name.slice(0, 1).toUpperCase() ?? "?") : "Ra"}
        </span>
        <span className="scope-name">
          {label}
          <small>
            {teamId
              ? "Team workspace"
              : current.role === "member"
                ? "Your review activity"
                : "Platform view"}
          </small>
        </span>
        <svg
          width="14"
          height="18"
          viewBox="0 0 16 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          aria-hidden="true"
        >
          <path d="m4 7 4-4 4 4M4 13l4 4 4-4" />
        </svg>
      </button>
      <div
        id={popoverId}
        ref={popover}
        popover="auto"
        className="team-picker"
        style={position}
        onToggle={(event) => {
          const isOpen = event.newState === "open";
          setOpen(isOpen);
          if (isOpen) {
            setDraft("");
            setTerm("");
            setAfter(0);
            input.current?.focus();
          }
        }}
      >
        <label className="field team-picker-search">
          <span className="sr-only">Find a team</span>
          <input
            ref={input}
            type="search"
            placeholder="Search teams…"
            maxLength={80}
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setAfter(0);
            }}
          />
        </label>
        <button
          className={`team-choice${teamId === null ? " selected" : ""}`}
          aria-pressed={teamId === null}
          onClick={() => select("")}
        >
          <span className="team-monogram" aria-hidden="true">
            Ra
          </span>
          <span>
            {current.role === "member" ? "All my teams" : "All teams"}
            <small>
              {current.role === "member"
                ? "Combined team activity"
                : "Platform view"}
            </small>
          </span>
        </button>
        <div className="team-choices" aria-label="Available teams">
          {(term || after) && matches.isFetching ? (
            <p role="status">Finding teams…</p>
          ) : null}
          {(term || after) && matches.isError ? (
            <Freshness query={matches} />
          ) : null}
          {page?.items.map((item) => (
            <button
              key={item.id}
              className={`team-choice${String(item.id) === teamId ? " selected" : ""}`}
              aria-pressed={String(item.id) === teamId}
              onClick={() => select(String(item.id))}
            >
              <span className="team-monogram" aria-hidden="true">
                {item.name.slice(0, 1).toUpperCase()}
              </span>
              <span>
                {item.name}
                <small>
                  {item.repository_count} repositories
                  {item.role ? ` · ${item.role}` : ""}
                </small>
              </span>
            </button>
          ))}
          {page?.items.length === 0 ? <p>No teams match this search.</p> : null}
          {page?.next_after_id ? (
            <button
              className="text-button"
              onClick={() => setAfter(page.next_after_id ?? 0)}
            >
              More teams
            </button>
          ) : null}
        </div>
        <Link
          className="team-picker-manage"
          to="/teams"
          onClick={() => popover.current?.hidePopover()}
        >
          {isAdmin(current.role) ? "Manage teams" : "Browse teams"}
        </Link>
      </div>
    </div>
  );
}
