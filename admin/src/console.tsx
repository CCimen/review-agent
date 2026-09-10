import { AppShell } from "@astryxdesign/core/AppShell";
import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import {
  HStack,
  Layout,
  LayoutContent,
  VStack,
} from "@astryxdesign/core/Layout";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
import { List, ListItem } from "@astryxdesign/core/List";
import { Section } from "@astryxdesign/core/Section";
import {
  SideNav,
  SideNavHeading,
  SideNavItem,
  SideNavSection,
  useSideNavCollapse,
} from "@astryxdesign/core/SideNav";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ChartColumn,
  BookOpen,
  Bot,
  Cable,
  FileClock,
  FolderGit2,
  GitPullRequest,
  HeartPulse,
  LogOut,
  Moon,
  Search,
  Settings,
  Sun,
  Users,
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { Account, BuildInfo, Overview, RepositoryPage } from "./api";
import { read } from "./api";
import {
  ScopeSelector,
  ScopedAnchor,
  contextualTo,
  isAdmin,
  roleLabels,
  useScope,
} from "./scope";
import { number, time } from "./ui";

const sections = [
  { path: "/", label: "Activity", icon: Activity, group: "Workspace" },
  {
    path: "/repositories",
    label: "Repositories",
    icon: FolderGit2,
    group: "Workspace",
  },
  {
    path: "/quality",
    label: "Review quality",
    icon: BookOpen,
    group: "Workspace",
  },
  { path: "/teams", label: "Teams", icon: Users, group: "Workspace" },
  {
    path: "/usage",
    label: "Usage",
    icon: ChartColumn,
    group: "Administration",
    admin: true,
  },
  {
    path: "/model-connections",
    label: "Model connections",
    icon: Bot,
    group: "Administration",
  },
  {
    path: "/operations",
    label: "Health",
    icon: HeartPulse,
    group: "Administration",
    admin: true,
  },
  {
    path: "/audit",
    label: "Audit log",
    icon: FileClock,
    group: "Administration",
    admin: true,
  },
  {
    path: "/settings",
    label: "Settings",
    icon: Settings,
    group: "Administration",
    owner: true,
  },
  {
    path: "/users",
    label: "Users",
    icon: Users,
    group: "Administration",
    admin: true,
  },
  {
    path: "/integrations",
    label: "Integrations",
    icon: Cable,
    group: "Administration",
    admin: true,
  },
];

function ExpandedRailContent({ children }: { children: ReactNode }) {
  return useSideNavCollapse().isCollapsed ? null : children;
}

export function ConsoleLayout({
  current,
  theme,
  toggleTheme,
  children,
  logout,
  signingOut,
}: {
  current: Account;
  theme: "light" | "dark";
  toggleTheme: () => void;
  children: ReactNode;
  logout: () => void;
  signingOut: boolean;
}) {
  const isNarrow = useMediaQuery("(max-width: 768px)");
  const scope = useScope();
  const build = useQuery({
    queryKey: ["build-info"],
    queryFn: ({ signal }) => read<BuildInfo>("/api/version", signal),
    staleTime: 60_000,
    refetchInterval: false,
  });
  const { pathname, search: routeSearch } = useLocation();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [searchTerm, setSearchTerm] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
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
    const timer = window.setTimeout(() => setSearchTerm(search.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [search]);
  function openPalette() {
    setSearch("");
    setSearchTerm("");
    setPaletteOpen(true);
  }
  function closePalette() {
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
        setPaletteOpen((open) => !open);
        setSearch("");
        setSearchTerm("");
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
          : (sections.find(
              (section) =>
                section.path !== "/" &&
                (pathname === section.path ||
                  pathname.startsWith(`${section.path}/`)),
            )?.path ?? "/");
  /* The breadcrumb named the section it fell back to, so an address the
     console does not serve read "Activity" above a page saying it was not
     found. These are the routes reached from somewhere other than the rail. */
  const offNav = ["/history", "/overview", "/access", "/account",
                  "/repository-requests"];
  const known =
    pathname === "/" ||
    offNav.includes(pathname) ||
    pathname.startsWith("/history/") ||
    pathname.startsWith("/findings/") ||
    sections.some(
      (section) =>
        section.path !== "/" &&
        (pathname === section.path || pathname.startsWith(`${section.path}/`)),
    );
  const title = !known
    ? "Page not found"
    : pathname.startsWith("/history/")
    ? "Review request"
    : pathname === "/history"
      ? "Pull requests"
      : pathname === "/overview"
        ? "Statistics"
        : pathname.startsWith("/findings/")
          ? "Finding"
          : pathname === "/repository-requests"
            ? "Repository requests"
            : pathname === "/access"
            ? "Repository access"
            : pathname === "/account"
              ? "Your account"
              : (sections.find((section) => section.path === activeSection)
                  ?.label ?? "Activity");
  const commands = [
    ...navigation,
    { path: "/history", label: "Pull requests", key: "P" },
    { path: "/overview", label: "Statistics", key: "T" },
    { path: "/account", label: "Your account", key: "U" },
  ].filter((section) =>
    section.label.toLowerCase().includes(search.toLowerCase()),
  );
  const iconProps = { size: "1em", "aria-hidden": true } as const;
  const workspaceLabel = scope.teamId
    ? (scope.team?.name ?? "Team workspace")
    : current.role === "member"
      ? "All my teams"
      : "All teams";
  return (
    <>
      <AppShell
        variant="elevated"
        height="auto"
        sideNav={
          <SideNav
            aria-label="Console sections"
            collapsible
            header={
              <SideNavHeading
                heading="Review Agent"
                subheading="Operator console"
                headingHref="/"
                as={ScopedAnchor}
                icon={<GitPullRequest {...iconProps} />}
              />
            }
            topContent={
              <ExpandedRailContent>
                <Section variant="transparent" padding={2}>
                  <ScopeSelector />
                </Section>
              </ExpandedRailContent>
            }
            footer={
              <ExpandedRailContent>
                <Section variant="transparent" padding={2}>
                  <VStack gap={3}>
                    <VStack gap={1}>
                      <Text type="supporting">Deployment</Text>
                      <Text weight="medium">
                        {typeof window === "undefined"
                          ? "Review Agent"
                          : window.location.hostname}
                      </Text>
                    </VStack>
                    {overview.data?.review_capacity != null && (
                      <VStack gap={1}>
                        <Text type="supporting">Online review capacity</Text>
                        <Text>
                          {number.format(overview.data.review_capacity)} slots ·{" "}
                          {number.format(
                            overview.data.live_review_workers ?? 0,
                          )}{" "}
                          workers
                        </Text>
                      </VStack>
                    )}
                    <VStack gap={1}>
                      <AstryxLink as={ScopedAnchor} href="/account">
                        {current.email}
                      </AstryxLink>
                      <Text type="supporting">
                        Your account · {roleLabels[current.role]}
                      </Text>
                    </VStack>
                  </VStack>
                </Section>
              </ExpandedRailContent>
            }
          >
            {(["Workspace", "Administration"] as const).map((group) => (
              <SideNavSection key={group} title={group}>
                {navigation
                  .filter((section) => section.group === group)
                  .map((section) => (
                    <SideNavItem
                      key={section.path}
                      label={section.label}
                      href={section.path}
                      as={ScopedAnchor}
                      isSelected={activeSection === section.path}
                      icon={<section.icon {...iconProps} />}
                      endContent={
                        section.path === "/" && overview.data ? (
                          <Badge
                            label={number.format(overview.data.active_requests)}
                          />
                        ) : undefined
                      }
                    />
                  ))}
              </SideNavSection>
            ))}
          </SideNav>
        }
      >
        <Section
          padding={6}
          paddingInline={isNarrow ? 4 : 6}
          paddingBlock={3}
          maxWidth={1440}
        >
          <HStack gap={3} wrap="wrap" align="center" justify="between">
            <Text color="secondary">
              {workspaceLabel} / {title}
            </Text>
            <HStack gap={2} wrap="wrap" align="center">
              <Button
                label="Search"
                icon={<Search {...iconProps} />}
                isIconOnly={isNarrow}
                variant="ghost"
                onClick={openPalette}
                tooltip="Search pages and repositories (⌘K / Ctrl+K)"
              />
              <Button
                label={theme === "light" ? "Dark theme" : "Light theme"}
                icon={
                  theme === "light" ? (
                    <Moon {...iconProps} />
                  ) : (
                    <Sun {...iconProps} />
                  )
                }
                isIconOnly={isNarrow}
                variant="ghost"
                onClick={toggleTheme}
                tooltip={theme === "light" ? "Dark theme" : "Light theme"}
              />
              <Button
                label={signingOut ? "Signing out…" : "Sign out"}
                icon={<LogOut {...iconProps} />}
                isIconOnly={isNarrow}
                variant="ghost"
                isDisabled={signingOut}
                onClick={logout}
                tooltip="Sign out"
              />
            </HStack>
          </HStack>
        </Section>
        {children}
        <Section
          padding={6}
          paddingInline={isNarrow ? 4 : 6}
          paddingBlock={3}
          maxWidth={1440}
        >
          <HStack as="footer" gap={4} wrap="wrap" align="center">
            <Text type="supporting">
              {build.data ? (
                <>
                  {build.data.version === "development"
                    ? "Development build"
                    : build.data.version}
                  {build.data.revision && (
                    <abbr title={`Source revision ${build.data.revision}`}>
                      {" "}
                      · {build.data.revision.slice(0, 7)}
                    </abbr>
                  )}
                </>
              ) : build.isError ? (
                <Button
                  label="Version unavailable · Retry"
                  variant="ghost"
                  size="sm"
                  onClick={() => void build.refetch()}
                  isDisabled={build.isFetching}
                />
              ) : (
                "Loading version…"
              )}
            </Text>
            <AstryxLink href="/api/docs" target="_blank" rel="noreferrer">
              API reference
            </AstryxLink>
            <Text type="supporting">
              Active requests{" "}
              {overview.data
                ? number.format(overview.data.active_requests)
                : "—"}
            </Text>
            <Text type="supporting">
              Repositories{" "}
              {overview.data
                ? number.format(overview.data.repository_count)
                : "—"}
            </Text>
            <Text type="supporting">
              {overview.isError
                ? "Connection interrupted"
                : overview.data
                  ? `Updated ${time(new Date(overview.dataUpdatedAt).toISOString())}`
                  : "Connecting…"}
            </Text>
          </HStack>
        </Section>
      </AppShell>
      <Dialog isOpen={paletteOpen} onOpenChange={setPaletteOpen} width={640}>
        <Layout
          height="auto"
          header={
            <DialogHeader
              title="Search the console"
              onOpenChange={setPaletteOpen}
            />
          }
          content={
            <LayoutContent>
              <VStack gap={3}>
                <TextInput
                  label="Search pages, repositories, or request ID"
                  value={search}
                  onChange={setSearch}
                  placeholder="Page, repository, or request ID"
                  startIcon="search"
                  hasClear
                  hasAutoFocus
                  width="100%"
                />
                <List density="compact" aria-label="Search results">
                  {commands.map((section) => (
                    <ListItem
                      key={section.path}
                      label={section.label}
                      onClick={() => go(section.path)}
                    />
                  ))}
                  {/^[1-9]\d{0,14}$/.test(search.trim()) && (
                    <ListItem
                      label={`Open request #${search.trim()}`}
                      onClick={() => go(`/history/${search.trim()}`)}
                    />
                  )}
                  {searchTerm.length >= 2 &&
                    repositories.data?.items.map((repo) => (
                      <ListItem
                        key={repo.repository}
                        label={repo.repository}
                        onClick={() =>
                          go(
                            `/history?repository=${encodeURIComponent(repo.repository)}`,
                          )
                        }
                      />
                    ))}
                  <ListItem
                    label={`Use ${theme === "light" ? "dark" : "light"} theme`}
                    onClick={() => {
                      toggleTheme();
                      closePalette();
                    }}
                  />
                </List>
                {searchTerm.length >= 2 && repositories.isFetching && (
                  <Text role="status">Searching repositories…</Text>
                )}
                {searchTerm.length >= 2 && repositories.isError && (
                  <Text role="alert">Repository search is unavailable.</Text>
                )}
              </VStack>
            </LayoutContent>
          }
        />
      </Dialog>
    </>
  );
}
