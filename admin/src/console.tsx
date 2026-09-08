import {
  ScopedLink as Link,
  ScopedNavLink as NavLink,
  useScope,
  ScopeSelector,
  isAdmin,
  roleLabels,
  contextualTo,
} from "./scope";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { read } from "./api";
import type { Account, Overview, RepositoryPage } from "./api";
import { number, time } from "./ui";

const sections = [
  { path: "/", label: "Activity", key: "A" },
  { path: "/repositories", label: "Repositories", key: "R" },
  { path: "/quality", label: "Review quality", key: "Q" },
  { path: "/operations", label: "Health", key: "H", admin: true },
  { path: "/teams", label: "Teams", key: "T" },
  { path: "/model-connections", label: "Model connections", key: "M" },
  { path: "/audit", label: "Audit log", key: "L", admin: true },
  { path: "/settings", label: "Settings", key: "S", owner: true },
  { path: "/users", label: "Users", key: "U", admin: true },
];

function initialTheme(): "light" | "dark" {
  try {
    const saved = localStorage.getItem("review-agent.theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* Storage can be disabled by the browser. */
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function ConsoleLayout({
  current,
  children,
  logout,
  signingOut,
}: {
  current: Account;
  children: ReactNode;
  logout: () => void;
  signingOut: boolean;
}) {
  const scope = useScope();
  const { pathname, search: routeSearch } = useLocation();
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const [theme, setTheme] = useState(initialTheme);
  const [search, setSearch] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const palette = useRef<HTMLDialogElement>(null);
  const navigation = sections.filter(
    (section) =>
      (!section.admin || isAdmin(current.role)) &&
      (!section.owner || current.role === "owner"),
  );
  const overview = useQuery({
    queryKey: ["overview", 30, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<Overview>(scope.path("/api/overview?days=30"), signal),
  });
  const repositories = useQuery({
    queryKey: ["command-repositories", searchTerm, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<RepositoryPage>(
        scope.path(
          `/api/repositories?limit=8&search=${encodeURIComponent(searchTerm)}`,
        ),
        signal,
      ),
    enabled: paletteOpen && searchTerm.length >= 2,
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("review-agent.theme", theme);
    } catch {
      /* Keep the current tab usable without storage. */
    }
  }, [theme]);
  useEffect(() => {
    const timer = window.setTimeout(() => setSearchTerm(search.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [search]);
  function openPalette() {
    setSearch("");
    setSearchTerm("");
    setPaletteOpen(true);
    palette.current?.showModal();
  }
  function closePalette() {
    palette.current?.close();
    setPaletteOpen(false);
  }
  function go(path: string) {
    closePalette();
    navigate(contextualTo(path, routeSearch));
  }
  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (palette.current?.open) palette.current.close();
        else {
          setSearch("");
          setSearchTerm("");
          setPaletteOpen(true);
          palette.current?.showModal();
        }
      }
    }
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);
  const activeSection =
    pathname === "/history" ||
    pathname.startsWith("/history/") ||
    pathname === "/overview"
      ? "/"
      : pathname === "/access"
        ? "/repositories"
        : pathname.startsWith("/findings/")
          ? "/quality"
          : pathname === "/users"
            ? "/settings"
            : pathname;
  const title = pathname.startsWith("/history/")
    ? "Review request"
    : pathname === "/history"
      ? "Pull requests"
      : pathname === "/overview"
        ? "Statistics"
        : pathname.startsWith("/findings")
          ? "Finding"
          : pathname === "/access"
            ? "Repository access"
            : pathname === "/users"
              ? "Users"
              : pathname === "/account"
                ? "Your account"
                : (sections.find((section) => section.path === pathname)
                    ?.label ?? "Activity");
  const commands = [
    ...navigation,
    { path: "/history", label: "Pull requests", key: "P" },
    { path: "/overview", label: "Statistics", key: "T" },
    { path: "/account", label: "Your account", key: "U" },
    ...(isAdmin(current.role)
      ? [{ path: "/users", label: "Manage users", key: "U" }]
      : []),
  ].filter((section) =>
    section.label.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className={`console${collapsed ? " rail-collapsed" : ""}`}>
      <aside className="console-rail">
        <Link className="brand" to="/" aria-label="Review Agent activity">
          <span className="brand-mark" aria-hidden="true">
            Ra
          </span>
          <span className="rail-label">
            Review Agent<span className="brand-subtitle">Operator console</span>
          </span>
        </Link>
        <div className="rail-deployment rail-label">
          <span className="muted">Deployment</span>
          <strong>{window.location.hostname}</strong>
          <span className="muted">Advisory pull-request reviews</span>
        </div>
        <div className="rail-label">
          <ScopeSelector />
        </div>
        <nav className="console-nav" aria-label="Console sections">
          {navigation.map((section) => (
            <NavLink
              key={section.path}
              to={section.path}
              end={section.path === "/"}
              className={activeSection === section.path ? "active" : undefined}
              title={section.label}
            >
              <span className="nav-key" aria-hidden="true">
                {section.key}
              </span>
              <span className="rail-label">{section.label}</span>
              {section.path === "/" && overview.data ? (
                <span className="nav-count rail-label">
                  {number.format(overview.data.active_requests)}
                </span>
              ) : null}
            </NavLink>
          ))}
        </nav>
        {overview.data?.review_capacity != null ? (
          <div className="rail-capacity rail-label">
            <span>Online review capacity</span>
            <strong>
              {overview.data
                ? number.format(overview.data.review_capacity ?? 0)
                : "—"}{" "}
              slots
            </strong>
            <small>
              {overview.data
                ? `${number.format(overview.data.live_review_workers ?? 0)} review workers online`
                : "Waiting for worker reports"}
            </small>
          </div>
        ) : null}
        <div className="rail-account">
          <Link className="rail-label" to="/account">
            {current.email}
            <small>Your account · {roleLabels[current.role]}</small>
          </Link>
          <button
            className="secondary icon-button"
            onClick={() => setTheme(theme === "light" ? "dark" : "light")}
            aria-label={`Use ${theme === "light" ? "dark" : "light"} theme`}
            title={`Use ${theme === "light" ? "dark" : "light"} theme`}
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              aria-hidden="true"
            >
              <circle cx="12" cy="12" r="8" />
              <path d="M12 4v16" />
              <path d="M12 4a8 8 0 0 1 0 16Z" fill="currentColor" />
            </svg>
          </button>
        </div>
      </aside>
      <div className="console-body">
        <header className="console-topbar">
          <button
            className="secondary icon-button rail-toggle"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
            aria-expanded={!collapsed}
          >
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              aria-hidden="true"
            >
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <path d="M9 4v16" />
            </svg>
          </button>
          <div className="console-crumb">
            <Link to="/">Console</Link>
            <span aria-hidden="true">/</span>
            <span>{title}</span>
          </div>
          <button className="secondary command-trigger" onClick={openPalette}>
            <span>Search or run a command</span>
            <kbd>⌘ K</kbd>
          </button>
          <button
            className="text-button quiet signout"
            disabled={signingOut}
            onClick={logout}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </header>
        <div className="mobile-scope">
          <ScopeSelector />
        </div>
        {children}
        <footer className="console-statusbar">
          <a href="/api/docs" target="_blank" rel="noreferrer">
            API reference
          </a>
          <span>
            Active requests{" "}
            <strong>
              {overview.data
                ? number.format(overview.data.active_requests)
                : "—"}
            </strong>
          </span>
          {overview.data?.live_review_workers != null ? (
            <span>
              Review workers online{" "}
              <strong>
                {overview.data
                  ? number.format(overview.data.live_review_workers ?? 0)
                  : "—"}
              </strong>
            </span>
          ) : null}
          <span>
            Repositories{" "}
            <strong>
              {overview.data
                ? number.format(overview.data.repository_count)
                : "—"}
            </strong>
          </span>
          <span className="statusbar-refresh">
            {overview.isError
              ? "Connection interrupted"
              : overview.data
                ? `Updated ${time(new Date(overview.dataUpdatedAt).toISOString())}`
                : "Connecting…"}
          </span>
        </footer>
      </div>
      <dialog
        ref={palette}
        className="command-palette"
        onClose={() => setPaletteOpen(false)}
        onClick={(event) => {
          if (event.target === event.currentTarget) closePalette();
        }}
      >
        <div className="command-input">
          <label className="sr-only" htmlFor="command-search">
            Search pages, repositories, or request ID
          </label>
          <input
            id="command-search"
            autoFocus
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Jump to a page, repository, or request ID"
          />
          <button className="text-button" onClick={closePalette}>
            Close
          </button>
        </div>
        <div className="command-results">
          {commands.map((section) => (
            <button key={section.path} onClick={() => go(section.path)}>
              <span>{section.label}</span>
              <small>Page</small>
            </button>
          ))}
          {/^[1-9]\d{0,14}$/.test(search.trim()) ? (
            <button onClick={() => go(`/history/${search.trim()}`)}>
              Open request #{search.trim()}
              <small>Request</small>
            </button>
          ) : null}
          {searchTerm.length >= 2 &&
            repositories.data?.items.map((repo) => (
              <button
                key={repo.repository}
                onClick={() =>
                  go(
                    `/history?repository=${encodeURIComponent(repo.repository)}`,
                  )
                }
              >
                <span>{repo.repository}</span>
                <small>Repository</small>
              </button>
            ))}
          {searchTerm.length >= 2 && repositories.isFetching ? (
            <p role="status">Searching repositories…</p>
          ) : null}
          {searchTerm.length >= 2 && repositories.isError ? (
            <p role="alert">Repository search is unavailable.</p>
          ) : null}
          <button
            onClick={() => {
              setTheme(theme === "light" ? "dark" : "light");
              closePalette();
            }}
          >
            <span>Use {theme === "light" ? "dark" : "light"} theme</span>
            <small>Appearance</small>
          </button>
        </div>
      </dialog>
    </div>
  );
}
