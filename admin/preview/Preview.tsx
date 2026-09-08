import { useState, type ReactNode } from "react";
import { AppShell } from "@astryxdesign/core/AppShell";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack, StackItem } from "@astryxdesign/core/Layout";
import { Section } from "@astryxdesign/core/Section";
import { Selector } from "@astryxdesign/core/Selector";
import {
  SideNav,
  SideNavHeading,
  SideNavItem,
  SideNavSection,
  useSideNavCollapse,
} from "@astryxdesign/core/SideNav";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import {
  Table,
  proportional,
  pixel,
  type TableColumn,
} from "@astryxdesign/core/Table";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { Theme } from "@astryxdesign/core/theme";
import {
  Activity,
  BookOpen,
  Bot,
  FileClock,
  FolderGit2,
  GitPullRequest,
  HeartPulse,
  Moon,
  Settings,
  Sun,
  Users,
} from "lucide-react";
import { neutralTheme } from "./theme/neutralTheme";

type Review = {
  id: number;
  title: string;
  repository: string;
  team: string;
  state: "Published" | "Reviewing" | "Queued" | "Failed";
  model: string;
  updated: string;
};

const reviews: Review[] = [
  {
    id: 248,
    title: "Handle expired application credentials",
    repository: "platform/api-gateway",
    team: "Platform engineering",
    state: "Reviewing",
    model: "Primary reviewer",
    updated: "Just now",
  },
  {
    id: 91,
    title: "Keep team context when navigating",
    repository: "platform/developer-portal",
    team: "Platform engineering",
    state: "Published",
    model: "Primary reviewer",
    updated: "8 minutes ago",
  },
  {
    id: 247,
    title: "Add a bounded reporting window",
    repository: "platform/api-gateway",
    team: "Platform engineering",
    state: "Published",
    model: "Primary reviewer",
    updated: "24 minutes ago",
  },
  {
    id: 63,
    title: "Clarify service ownership",
    repository: "platform/service-catalog",
    team: "Platform engineering",
    state: "Queued",
    model: "Primary reviewer",
    updated: "32 minutes ago",
  },
  {
    id: 90,
    title: "Preserve drafts during background refresh",
    repository: "platform/developer-portal",
    team: "Platform engineering",
    state: "Published",
    model: "Primary reviewer",
    updated: "1 hour ago",
  },
  {
    id: 122,
    title: "Validate report export filters",
    repository: "data/reporting",
    team: "Data services",
    state: "Failed",
    model: "Reporting reviewer",
    updated: "2 hours ago",
  },
  {
    id: 62,
    title: "Document the repository access model",
    repository: "platform/service-catalog",
    team: "Platform engineering",
    state: "Published",
    model: "Primary reviewer",
    updated: "3 hours ago",
  },
  {
    id: 121,
    title: "Use the new reporting endpoint",
    repository: "data/reporting",
    team: "Data services",
    state: "Published",
    model: "Reporting reviewer",
    updated: "4 hours ago",
  },
];

const statusVariants = {
  Published: "success",
  Reviewing: "accent",
  Queued: "neutral",
  Failed: "error",
} as const;
const columns: TableColumn<Review>[] = [
  {
    key: "title",
    header: "Pull request",
    width: proportional(3, { minWidth: 260 }),
    renderCell: (row) => (
      <VStack gap={1}>
        <Text weight="medium">{row.title}</Text>
        <Text type="supporting">
          {row.repository} · #{row.id}
        </Text>
      </VStack>
    ),
  },
  {
    key: "state",
    header: "Status",
    width: pixel(140),
    renderCell: (row) => (
      <HStack gap={2} align="center">
        <StatusDot variant={statusVariants[row.state]} label={row.state} />
        <Text>{row.state}</Text>
      </HStack>
    ),
  },
  {
    key: "model",
    header: "Model connection",
    width: proportional(1, { minWidth: 170 }),
  },
  {
    key: "updated",
    header: "Updated",
    width: pixel(140),
    renderCell: (row) => <Text color="secondary">{row.updated}</Text>,
  },
];

// SideNav collapses its own items; custom slots must follow that state.
function ExpandedRailContent({ children }: { children: ReactNode }) {
  const { isCollapsed } = useSideNavCollapse();
  return isCollapsed ? null : children;
}

export function Preview() {
  const [mode, setMode] = useState<"light" | "dark">("light");
  const [team, setTeam] = useState("All teams");
  const [status, setStatus] = useState("All statuses");
  const [search, setSearch] = useState("");
  const filtered = reviews.filter(
    (row) =>
      (team === "All teams" || row.team === team) &&
      (status === "All statuses" || row.state === status) &&
      `${row.title} ${row.repository} ${row.id}`
        .toLowerCase()
        .includes(search.trim().toLowerCase()),
  );
  const iconProps = { size: "1em", "aria-hidden": true } as const;

  return (
    <Theme theme={neutralTheme} mode={mode}>
      <AppShell
        variant="elevated"
        height="fill"
        sideNav={
          <SideNav
            aria-label="Console sections"
            collapsible
            header={
              <SideNavHeading
                heading="Review Agent"
                subheading="Operator console"
                icon={<GitPullRequest {...iconProps} />}
              />
            }
            topContent={
              <ExpandedRailContent>
                <Section variant="transparent" padding={2}>
                  <Selector
                    label="Team workspace"
                    value={team}
                    onChange={setTeam}
                    options={[
                      "All teams",
                      "Platform engineering",
                      "Data services",
                    ]}
                    hasSearch
                    width="100%"
                  />
                </Section>
              </ExpandedRailContent>
            }
            footer={
              <ExpandedRailContent>
                <Section variant="transparent" padding={2}>
                  <VStack gap={2}>
                    <Text weight="medium">Design preview</Text>
                    <Text type="supporting">
                      Illustrative data · no live connection
                    </Text>
                  </VStack>
                </Section>
              </ExpandedRailContent>
            }
          >
            <SideNavSection title="Workspace">
              <SideNavItem
                label="Activity"
                icon={<Activity {...iconProps} />}
                isSelected
                onClick={() => {
                  setSearch("");
                  setStatus("All statuses");
                }}
              />
              <SideNavItem
                label="Repositories"
                icon={<FolderGit2 {...iconProps} />}
                isDisabled
              />
              <SideNavItem
                label="Review quality"
                icon={<BookOpen {...iconProps} />}
                isDisabled
              />
              <SideNavItem
                label="Teams"
                icon={<Users {...iconProps} />}
                isDisabled
              />
            </SideNavSection>
            <SideNavSection title="Administration">
              <SideNavItem
                label="Model connections"
                icon={<Bot {...iconProps} />}
                isDisabled
              />
              <SideNavItem
                label="Health"
                icon={<HeartPulse {...iconProps} />}
                isDisabled
              />
              <SideNavItem
                label="Audit log"
                icon={<FileClock {...iconProps} />}
                isDisabled
              />
              <SideNavItem
                label="Settings"
                icon={<Settings {...iconProps} />}
                isDisabled
              />
            </SideNavSection>
          </SideNav>
        }
      >
        <Section padding={6}>
          <VStack gap={6}>
            <HStack justify="between" align="center" wrap="wrap" gap={3}>
              <Text color="secondary">{team} / Activity</Text>
              <Button
                label={mode === "light" ? "Dark theme" : "Light theme"}
                icon={
                  mode === "light" ? (
                    <Moon {...iconProps} />
                  ) : (
                    <Sun {...iconProps} />
                  )
                }
                variant="ghost"
                onClick={() => setMode(mode === "light" ? "dark" : "light")}
              />
            </HStack>
            <VStack gap={2}>
              <Heading level={1}>Review activity</Heading>
              <Text color="secondary">
                Follow pull requests from a review request to published
                feedback.
              </Text>
            </VStack>
            <HStack gap={3} align="end" wrap="wrap">
              <StackItem size="fill">
                <TextInput
                  label="Search pull requests"
                  placeholder="Title, repository, or pull request number"
                  value={search}
                  onChange={setSearch}
                  startIcon="search"
                  hasClear
                  width="100%"
                />
              </StackItem>
              <Selector
                label="Status"
                options={[
                  "All statuses",
                  "Reviewing",
                  "Queued",
                  "Published",
                  "Failed",
                ]}
                value={status}
                onChange={setStatus}
              />
              {(search || status !== "All statuses") && (
                <Button
                  label="Clear filters"
                  variant="ghost"
                  onClick={() => {
                    setSearch("");
                    setStatus("All statuses");
                  }}
                />
              )}
            </HStack>
            <VStack gap={3}>
              <HStack justify="between" align="center" wrap="wrap" gap={2}>
                <Text weight="medium">Recent requests</Text>
                <Text type="supporting" aria-live="polite">
                  {filtered.length} of {reviews.length} sample requests
                </Text>
              </HStack>
              <Table
                aria-label="Sample review requests"
                data={filtered}
                columns={columns}
                idKey="id"
                density="balanced"
                hasHover
                emptyState={
                  <Section padding={8}>
                    <VStack gap={3}>
                      <Heading level={2}>No requests match</Heading>
                      <Text color="secondary">
                        Try another search, status, or team.
                      </Text>
                      <Button
                        label="Clear filters"
                        onClick={() => {
                          setSearch("");
                          setStatus("All statuses");
                        }}
                      />
                    </VStack>
                  </Section>
                }
              />
            </VStack>
            <Text type="supporting">
              Activity is the preview screen. Other destinations become
              available when this design is applied to the console.
            </Text>
          </VStack>
        </Section>
      </AppShell>
    </Theme>
  );
}
