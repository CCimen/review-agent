import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { useMediaQuery } from "@astryxdesign/core/hooks";
import { Section as AstryxSection } from "@astryxdesign/core/Section";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
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
import { TextInput } from "@astryxdesign/core/TextInput";
import { Heading, Text } from "@astryxdesign/core/Text";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import documentationStarter from "../../examples/repository-context/.review-agent/documentation.toml?raw";
import { APIError, read, write } from "./api";
import type { RepositoryPage } from "./api";
import type { components } from "./api.generated";
import { ScopedLink as Link, useScope } from "./scope";
import {
  Copy,
  Empty,
  ExternalLink,
  Form,
  Freshness,
  Loading,
  Saved,
  time,
  useFilters,
  useLiveSearch,
} from "./ui";

type Mode = components["schemas"]["DocumentationMode"];
type ResolvedMode = components["schemas"]["ResolvedDocumentationMode"];
type TeamPolicy = components["schemas"]["TeamDocumentationPolicy"];
type RepositoryPolicy = components["schemas"]["RepositoryDocumentationPolicy"];
type OwnershipPreview = components["schemas"]["OwnershipDocumentationPreview"];

const modeOptions = [
  { value: "off", label: "Off" },
  { value: "manual", label: "Manual" },
  { value: "automatic", label: "Automatic" },
];
export function documentationModeLabel(mode: Mode) {
  return modeOptions.find((option) => option.value === mode)?.label ?? mode;
}

export function DocumentationReviewEvidence({
  result,
  repository,
}: {
  result: components["schemas"]["DocumentationReviewSummary"];
  repository: string;
}) {
  const isNarrow = useMediaQuery("(max-width: 768px)");
  const labels: Record<components["schemas"]["DocumentationOutcome"], string> =
    {
      not_needed: "No relevant documentation changes",
      no_mismatch_found: "No mismatch found in the selected scope",
      findings: "Documentation changes suggested",
      incomplete: "Documentation review incomplete",
      not_configured: "No merged documentation rules",
      invalid_configuration: "Documentation rules need correction",
      unavailable: "Documentation evidence unavailable",
    };
  const source = (revision: string, path: string) =>
    `https://github.com/${repository}/blob/${revision}/${path.split("/").map(encodeURIComponent).join("/")}`;
  const incomplete =
    result.outcome !== null &&
    ["incomplete", "invalid_configuration", "unavailable"].includes(
      result.outcome,
    );
  return (
    <VStack gap={4}>
      {incomplete ? (
        <Banner
          status="warning"
          title={labels[result.outcome!]}
          description="The selected documentation could not be fully assessed. Review the recorded gaps before relying on this result."
        />
      ) : (
        <Heading level={3}>
          {result.outcome
            ? labels[result.outcome]
            : "Documentation assessment in progress"}
        </Heading>
      )}
      <Text as="p" color="secondary">
        {result.outcome === "not_needed"
          ? "No documentation assessment was needed for the matched changes."
          : result.coverage_complete
            ? "Selected documentation coverage is complete."
            : "Selected documentation coverage is not complete."}{" "}
        This result applies to the recorded change and scope.{" "}
        {result.outcome === null
          ? "The assessment is not final."
          : result.semantic_inference_used
            ? "Model-assisted assessment was used."
            : "No model-assisted assessment was used."}
      </Text>
      {result.incomplete_reasons.length > 0 && (
        <VStack as="ul" gap={2}>
          {result.incomplete_reasons.map((reason) => (
            <li key={reason}>{reason.replaceAll("_", " ")}</li>
          ))}
        </VStack>
      )}
      <Collapsible
        defaultIsOpen={false}
        trigger="Documentation scope and evidence"
      >
        <VStack gap={4}>
          <MetadataList
            columns={isNarrow ? "single" : "multi"}
            label={{ position: isNarrow ? "start" : "top" }}
          >
            <MetadataListItem label="Policy at target base">
              <ExternalLink
                href={source(
                  result.base_sha,
                  ".review-agent/documentation.toml",
                )}
              >
                <Code>{result.base_sha.slice(0, 12)}</Code>
              </ExternalLink>
            </MetadataListItem>
            <MetadataListItem label="Comparison commit">
              {result.comparison_sha ? (
                <ExternalLink
                  href={`https://github.com/${repository}/commit/${result.comparison_sha}`}
                >
                  <Code>{result.comparison_sha.slice(0, 12)}</Code>
                </ExternalLink>
              ) : (
                "Unavailable"
              )}
            </MetadataListItem>
            <MetadataListItem label="Reviewed head">
              <ExternalLink
                href={`https://github.com/${repository}/commit/${result.head_sha}`}
              >
                <Code>{result.head_sha.slice(0, 12)}</Code>
              </ExternalLink>
            </MetadataListItem>
          </MetadataList>
          {result.scope ? (
            <>
              <Heading level={4}>
                Selected documents · {result.scope.documents.length}
              </Heading>
              {result.scope.documents.length ? (
                <VStack as="ul" gap={2}>
                  {result.scope.documents.map((path) => (
                    <li key={path}>
                      <ExternalLink href={source(result.head_sha, path)}>
                        {path}
                      </ExternalLink>
                    </li>
                  ))}
                </VStack>
              ) : (
                <Text color="secondary">No documents selected.</Text>
              )}
              {result.scope.areas.map((area) => (
                <VStack key={area.id} gap={1}>
                  <Text weight="semibold">{area.id}</Text>
                  <Text as="p">{area.intent}</Text>
                  <Text type="supporting">
                    Matched changes: {area.matched_paths.join(", ")}
                  </Text>
                </VStack>
              ))}
              {result.scope.exclusions.length > 0 && (
                <>
                  <Heading level={4}>Excluded paths</Heading>
                  <VStack as="ul" gap={2}>
                    {result.scope.exclusions.map((item, index) => (
                      <li key={`${item.path}:${index}`}>
                        {item.path} · {item.reason}
                      </li>
                    ))}
                  </VStack>
                </>
              )}
              {result.scope.unmapped_paths.length > 0 && (
                <>
                  <Heading level={4}>Unmapped changes</Heading>
                  <Text>{result.scope.unmapped_paths.join(", ")}</Text>
                </>
              )}
            </>
          ) : (
            <Text color="secondary">
              A documentation scope was not established.
            </Text>
          )}
          <Heading level={4}>
            Recorded source reads · {result.evidence.length}
          </Heading>
          <Text as="p" color="secondary">
            These are the exact revisions and line ranges returned during this
            request, including unavailable evidence. Policy and ADR reads may
            differ from the repository's current default branch.
          </Text>
          <VStack as="ul" gap={2}>
            {result.evidence.map((entry, index) => (
              <li key={index}>
                <ExternalLink
                  href={`${source(entry.revision, entry.path)}${entry.start_line === null ? "" : `#L${entry.start_line}${entry.end_line === entry.start_line ? "" : `-L${entry.end_line}`}`}`}
                >
                  {entry.path}
                </ExternalLink>{" "}
                · {entry.role} · {entry.revision.slice(0, 12)}
                {entry.unavailable_reason
                  ? ` · ${entry.unavailable_reason.replaceAll("_", " ")}`
                  : entry.total_lines === 0
                    ? " · Empty file"
                    : ` · Lines ${entry.start_line}–${entry.end_line} of ${entry.total_lines}`}
              </li>
            ))}
          </VStack>
        </VStack>
      </Collapsible>
    </VStack>
  );
}
function modeSource(mode: ResolvedMode) {
  return mode.source === "repository"
    ? "Repository override"
    : mode.source === "team"
      ? "Team default"
      : "Unassigned default";
}
function EffectiveMode({ mode }: { mode: ResolvedMode }) {
  return (
    <VStack gap={1}>
      <Text>{documentationModeLabel(mode.effective_mode)}</Text>
      <Text type="supporting" color="secondary">
        {modeSource(mode)}
        {!mode.deployment_enabled
          ? ` · ${documentationModeLabel(mode.configured_mode)} when enabled`
          : ""}
      </Text>
    </VStack>
  );
}
function ModeHelp() {
  return (
    <Text as="p" color="secondary">
      Manual runs when a developer posts /review docs. Automatic also checks
      eligible pull request changes. Off prevents documentation reviews.
      Repository rules determine which documents and changes are relevant.
    </Text>
  );
}
function DeploymentPaused({ enabled }: { enabled: boolean }) {
  const scope = useScope();
  return !enabled ? (
    <Banner
      status="info"
      title="Documentation reviews are disabled for this deployment"
      description="Your mode is saved, but reviews remain off until an owner enables documentation reviews in Settings."
      /* An owner can act on this here rather than hunting for the switch. */
      endContent={
        scope.current.role === "owner" ? (
          <Link to="/settings">Open Settings</Link>
        ) : undefined
      }
    />
  ) : null;
}
function usePolicyRefresh() {
  const client = useQueryClient();
  return () =>
    Promise.all(
      [
        "team-documentation",
        "repository-documentation",
        "ownership-documentation",
        "team-repositories",
        "repositories",
        "teams",
        "team",
        "audit",
        "me",
      ].map((key) => client.invalidateQueries({ queryKey: [key] })),
    );
}

export function TeamDocumentationSection({ teamId }: { teamId: number }) {
  const scope = useScope();
  const query = useQuery({
    queryKey: ["team-documentation", teamId, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPolicy>(
        scope.path(`/api/teams/${teamId}/documentation`),
        signal,
      ),
    refetchOnWindowFocus: false,
  });
  return (
    <VStack gap={4} as="section">
      <Heading level={2}>Documentation reviews</Heading>
      <ModeHelp />
      <Freshness query={query} interval={false} />
      {query.data && <TeamDocumentationEditor initial={query.data} />}
    </VStack>
  );
}

function TeamDocumentationEditor({ initial }: { initial: TeamPolicy }) {
  const scope = useScope();
  const refresh = usePolicyRefresh();
  const [mode, setMode] = useState<Mode>(initial.default_mode);
  const [preview, setPreview] = useState(initial);
  const [after, setAfter] = useState(0);
  const [stale, setStale] = useState(false);
  const [saved, setSaved] = useState(false);
  const load = useMutation({
    mutationFn: (after: number) =>
      read<TeamPolicy>(
        scope.path(
          `/api/teams/${initial.team_id}/documentation?proposed_mode=${mode}&after_id=${after}`,
        ),
        new AbortController().signal,
      ),
    onSuccess: (data, after) => {
      if (after && data.revision !== preview.revision) {
        setStale(true);
        return;
      }
      setPreview(data);
      setAfter(after);
      setStale(false);
      setSaved(false);
    },
  });
  const save = useMutation({
    mutationFn: () =>
      write<TeamPolicy>(
        scope.path(`/api/teams/${initial.team_id}/documentation`),
        "PUT",
        {
          mode,
          expected_revision: preview.revision,
        } satisfies components["schemas"]["TeamDocumentationUpdate"],
      ),
    onSuccess: async (data) => {
      setPreview(data);
      setAfter(0);
      setMode(data.default_mode);
      setSaved(true);
      await refresh();
    },
    onError: (error) => {
      if (error instanceof APIError && error.status === 409) setStale(true);
    },
  });
  const busy = load.isPending || save.isPending;
  const dirty = mode !== preview.default_mode;
  const reviewed = !stale && preview.proposed_mode === mode;
  const columns: TableColumn<TeamPolicy["repositories"][number]>[] = [
    {
      key: "repository",
      header: "Repository",
      width: proportional(2, { minWidth: 220 }),
      renderCell: (row) => (
        <Link
          to={`/repositories?documentation_repository=${row.repository_id}`}
        >
          {row.repository}
        </Link>
      ),
    },
    {
      key: "repository_override",
      header: "Policy",
      width: proportional(1, { minWidth: 160 }),
      renderCell: (row) =>
        row.repository_override === null
          ? "Inherits team default"
          : `Exception · ${documentationModeLabel(row.repository_override)}`,
    },
    {
      key: "before",
      header: "Current effective mode",
      width: proportional(1, { minWidth: 150 }),
      renderCell: (row) => <EffectiveMode mode={row.before} />,
    },
    {
      key: "after",
      header: "After saving",
      width: proportional(1, { minWidth: 150 }),
      renderCell: (row) => <EffectiveMode mode={row.after} />,
    },
  ];
  return (
    <VStack gap={4}>
      <DeploymentPaused enabled={preview.deployment_enabled} />
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (reviewed && dirty && preview.can_manage) save.mutate();
        }}
      >
        <VStack gap={4}>
          {/* Three mutually exclusive modes, all visible: the choice is what
              this section is for, so it should not be hidden behind a menu. */}
          <VStack gap={2}>
            <Text>Team default</Text>
            <HStack hAlign="start">
              <SegmentedControl
                label="Team documentation review default"
                value={mode}
                isDisabled={busy || !preview.can_manage}
                disabledMessage={
                  preview.can_manage
                    ? undefined
                    : "A team maintainer or platform administrator can change this default."
                }
                onChange={(value) => {
                  setMode(value as Mode);
                  setSaved(false);
                  save.reset();
                }}
              >
                {modeOptions.map((option) => (
                  <SegmentedControlItem
                    key={option.value}
                    value={option.value}
                    label={option.label}
                  />
                ))}
              </SegmentedControl>
            </HStack>
          </VStack>
          <Text color="secondary">
            Applies to {preview.inherited_count} inherited{" "}
            {preview.inherited_count === 1 ? "repository" : "repositories"}.{" "}
            {preview.exception_count}{" "}
            {preview.exception_count === 1
              ? "repository has"
              : "repositories have"}{" "}
            an explicit override and will keep it.
          </Text>
          {preview.can_manage && (
            <HStack gap={3} wrap="wrap">
              <Button
                label={stale ? "Refresh impact preview" : "Preview impact"}
                isDisabled={busy}
                isLoading={load.isPending}
                onClick={() => {
                  save.reset();
                  load.mutate(0);
                }}
              />
              <Button
                label="Save team default"
                type="submit"
                variant="primary"
                isDisabled={busy || !dirty || !reviewed}
                isLoading={save.isPending}
              />
            </HStack>
          )}
          {!preview.can_manage && (
            <Text type="supporting">
              A team maintainer or platform administrator can change this
              default.
            </Text>
          )}
          {stale && (
            <Banner
              status="warning"
              title="The policy changed while you were editing"
              description="Your selection is preserved. Refresh the impact preview, review the changes, and save again."
            />
          )}
          {load.isError && (
            <Banner
              status="error"
              title="Could not load the impact preview"
              description={load.error.message}
            />
          )}
          {save.isError && !stale && (
            <Banner
              status="error"
              title="Could not save the team default"
              description={save.error.message}
            />
          )}
          {saved && <Saved>Team default saved.</Saved>}
        </VStack>
      </Form>
      {reviewed ? (
        <>
          <Heading level={3}>Repository impact</Heading>
          {preview.repositories.length ? (
            <Table
              data={preview.repositories}
              columns={columns}
              idKey="repository_id"
              verticalAlign="top"
            />
          ) : (
            <Text color="secondary">
              No repositories are assigned to this team.
            </Text>
          )}
          {(after > 0 || preview.next_after_id !== null) && (
            <HStack gap={3}>
              <Button
                label="First repositories"
                isDisabled={busy || after === 0}
                onClick={() => load.mutate(0)}
              />
              <Button
                label="More repositories"
                isDisabled={busy || preview.next_after_id === null}
                onClick={() => {
                  if (preview.next_after_id !== null)
                    load.mutate(preview.next_after_id);
                }}
              />
            </HStack>
          )}
        </>
      ) : (
        <Text color="secondary">
          Preview the selected mode to see its effect on repositories before
          saving.
        </Text>
      )}
    </VStack>
  );
}

export function RepositoryDocumentationSection({
  repositoryId,
  close,
}: {
  repositoryId: number;
  close?: () => void;
}) {
  const scope = useScope();
  const query = useQuery({
    queryKey: ["repository-documentation", repositoryId, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<RepositoryPolicy>(
        scope.path(`/api/repositories/${repositoryId}/documentation`),
        signal,
      ),
    refetchOnWindowFocus: false,
  });
  return (
    <VStack as="section" gap={4}>
      <HStack gap={3} wrap="wrap" justify="between" align="center">
        <Heading level={2}>
          Documentation reviews{query.data ? ` · ${query.data.repository}` : ""}
        </Heading>
        {close && (
          <Button
            label="Close documentation settings"
            variant="ghost"
            onClick={close}
          />
        )}
      </HStack>
      <Freshness query={query} interval={false} />
      {query.data && (
        <RepositoryDocumentationEditor
          key={`${repositoryId}:${scope.key}`}
          initial={query.data}
        />
      )}
    </VStack>
  );
}

function RepositoryDocumentationEditor({
  initial,
}: {
  initial: RepositoryPolicy;
}) {
  const scope = useScope();
  const refresh = usePolicyRefresh();
  const isNarrow = useMediaQuery("(max-width: 768px)");
  const [policy, setPolicy] = useState(initial);
  const [mode, setMode] = useState<Mode | "inherit">(
    initial.repository_override ?? "inherit",
  );
  const [stale, setStale] = useState(false);
  const [saved, setSaved] = useState(false);
  const path = scope.path(
    `/api/repositories/${policy.repository_id}/documentation`,
  );
  const reload = useMutation({
    mutationFn: () =>
      read<RepositoryPolicy>(path, new AbortController().signal),
    onSuccess: (data) => {
      setPolicy(data);
      setStale(false);
      save.reset();
    },
  });
  const save = useMutation({
    mutationFn: () =>
      write<RepositoryPolicy>(path, "PUT", {
        mode: mode === "inherit" ? null : mode,
        expected_revision: policy.revision,
      } satisfies components["schemas"]["RepositoryDocumentationUpdate"]),
    onSuccess: async (data) => {
      setPolicy(data);
      setMode(data.repository_override ?? "inherit");
      setSaved(true);
      await refresh();
    },
    onError: (error) => {
      if (error instanceof APIError && error.status === 409) setStale(true);
    },
  });
  const refreshConfiguration = useMutation({
    mutationFn: () =>
      write<RepositoryPolicy>(
        scope.path(
          `/api/repositories/${policy.repository_id}/documentation/refresh`,
        ),
        "POST",
      ),
    onSuccess: async (data) => {
      setStale(data.revision !== policy.revision);
      setPolicy(data);
      await refresh();
    },
  });
  const busy =
    save.isPending || reload.isPending || refreshConfiguration.isPending;
  return (
    <VStack gap={4}>
      <MetadataList
        label={{ position: isNarrow ? "start" : "top" }}
        columns={isNarrow ? "single" : "multi"}
      >
        <MetadataListItem label="Effective mode">
          <EffectiveMode mode={policy.resolved} />
        </MetadataListItem>
        <MetadataListItem label="Owning team">
          {policy.team_id ? (
            <Link
              to={`/teams/${policy.team_id}?team_id=${policy.team_id}&tab=documentation`}
            >
              {policy.team_name}
            </Link>
          ) : (
            "Unassigned"
          )}
        </MetadataListItem>
        <MetadataListItem label="Checks permission">
          {
            {
              not_checked: "Not checked",
              ready: "Granted",
              missing_checks: "Checks permission approval needed",
              access_unavailable: "Repository access unavailable",
            }[policy.capability]
          }
        </MetadataListItem>
        <MetadataListItem label="Repository configuration">
          {
            {
              not_checked: "Not checked",
              valid: "Rules parsed",
              not_configured: "Waiting for merged documentation rules",
              invalid: "Rules need correction",
              unavailable: "Configuration unavailable",
            }[policy.configuration]
          }
        </MetadataListItem>
      </MetadataList>
      <DeploymentPaused enabled={policy.resolved.deployment_enabled} />
      <ModeHelp />
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (!stale && policy.can_manage) save.mutate();
        }}
      >
        <VStack gap={4}>
          <VStack gap={2}>
            <Text>Repository mode</Text>
            <HStack hAlign="start">
              <SegmentedControl
                label="Repository documentation review mode"
                value={mode}
                isDisabled={busy || !policy.can_manage}
                disabledMessage={
                  policy.can_manage
                    ? undefined
                    : "A repository maintainer or platform administrator can change this mode."
                }
                onChange={(value) => {
                  setMode(value as Mode | "inherit");
                  setSaved(false);
                  save.reset();
                }}
              >
                <SegmentedControlItem value="inherit" label="Inherit" />
                {modeOptions.map((option) => (
                  <SegmentedControlItem
                    key={option.value}
                    value={option.value}
                    label={option.label}
                  />
                ))}
              </SegmentedControl>
            </HStack>
            {/* What "Inherit" resolves to belongs beside the choice, not
                inside a segment label that would crowd the other three. */}
            <Text type="supporting">
              Inherit follows the {policy.team_id ? "team" : "unassigned"}{" "}
              default, {documentationModeLabel(policy.team_default ?? "manual")}
              .
            </Text>
          </VStack>
          {policy.can_manage ? (
            <HStack gap={3} wrap="wrap">
              <Button
                label="Save repository mode"
                type="submit"
                variant="primary"
                isDisabled={
                  busy ||
                  stale ||
                  mode === (policy.repository_override ?? "inherit")
                }
                isLoading={save.isPending}
              />
              {stale && (
                <Button
                  label="Refresh policy and keep selection"
                  isDisabled={busy}
                  onClick={() => reload.mutate()}
                />
              )}
            </HStack>
          ) : (
            <Text type="supporting">
              A team maintainer or platform administrator can change this mode.
            </Text>
          )}
          {stale && (
            <Banner
              status="warning"
              title="The repository policy changed"
              description="Your selection is preserved. Refresh the policy, review the effective mode and ownership, then save again."
            />
          )}
          {save.isError && !stale && (
            <Banner
              status="error"
              title="Could not save the repository mode"
              description={save.error.message}
            />
          )}
          {reload.isError && (
            <Banner
              status="error"
              title="Could not refresh the repository policy"
              description={reload.error.message}
            />
          )}
          {saved && <Saved>Repository mode saved.</Saved>}
        </VStack>
      </Form>
      <VStack gap={2}>
        <Heading level={3}>Rules and review scope</Heading>
        <Text as="p" color="secondary">
          Documentation rules live with the code in .review-agent/. Changes to
          rules and ADRs are reviewed in GitHub. Saving a mode here does not
          verify the repository configuration or GitHub access.
        </Text>
        <HStack gap={3} wrap="wrap">
          {policy.can_manage && (
            <Button
              label="Refresh configuration"
              variant="secondary"
              isDisabled={busy}
              isLoading={refreshConfiguration.isPending}
              onClick={() => refreshConfiguration.mutate()}
            />
          )}
          <Link
            to={
              policy.configuration_detail?.source_url ??
              `https://github.com/${policy.repository}/blob/HEAD/.review-agent/documentation.toml`
            }
          >
            Open repository rules on GitHub
          </Link>
          <Copy value={documentationStarter} label="Copy starter configuration">
            Starter configuration
          </Copy>
          <Copy value="/review docs" label="Copy documentation review command">
            /review docs
          </Copy>
          <Link
            to={`/history?${new URLSearchParams({ repository: policy.repository, purpose: "documentation" })}`}
          >
            Documentation review history
          </Link>
        </HStack>
        <Text as="p" color="secondary">
          Refresh reads the default branch without using a model. It validates
          rules and relationships; the review checks selected documents at the
          pull request's exact revisions. Adapt the starter paths to this
          repository and merge the file as .review-agent/documentation.toml.
        </Text>
        {refreshConfiguration.isError && (
          <Banner
            status="error"
            title="Could not refresh the repository configuration"
            description={refreshConfiguration.error.message}
          />
        )}
        {policy.configuration_detail && (
          <>
            <Text type="supporting">
              Read {time(policy.configuration_detail.read_at)}
              {policy.configuration_detail.default_branch
                ? ` · ${policy.configuration_detail.default_branch}`
                : ""}
              {policy.configuration_detail.revision
                ? ` · ${policy.configuration_detail.revision.slice(0, 12)}`
                : ""}
            </Text>
            {policy.configuration_detail.problem && (
              <Text as="p" role="status">
                {policy.configuration_detail.problem}
              </Text>
            )}
            {policy.configuration_detail.policy && (
              <Collapsible
                defaultIsOpen={false}
                trigger="Mappings and exclusions"
              >
                <VStack gap={4}>
                  {policy.configuration_detail.policy.areas.map((area) => (
                    <VStack gap={2} key={area.id}>
                      <Heading level={4}>{area.id}</Heading>
                      <Text as="p">{area.intent}</Text>
                      <Text>Changed sources: {area.sources.join(", ")}</Text>
                      <Text>
                        Maintained documents: {area.documents.join(", ")}
                      </Text>
                    </VStack>
                  ))}
                  {policy.configuration_detail.policy.ignore_changes.length >
                    0 && (
                    <>
                      <Heading level={4}>Ignored changes</Heading>
                      {policy.configuration_detail.policy.ignore_changes.map(
                        (rule, index) => (
                          <Text as="p" key={index}>
                            {rule.paths.join(", ")} · {rule.reason}
                          </Text>
                        ),
                      )}
                    </>
                  )}
                  {policy.configuration_detail.policy.ignore_documents.length >
                    0 && (
                    <>
                      <Heading level={4}>Excluded documents</Heading>
                      {policy.configuration_detail.policy.ignore_documents.map(
                        (rule, index) => (
                          <Text as="p" key={index}>
                            {rule.paths.join(", ")} · {rule.reason}
                          </Text>
                        ),
                      )}
                    </>
                  )}
                </VStack>
              </Collapsible>
            )}
          </>
        )}
      </VStack>
    </VStack>
  );
}

export function OwnershipDocumentationAction({
  repositoryId,
  destinationTeamId,
  expectedTeamId,
  label,
  description,
  done,
  width,
}: {
  repositoryId: number;
  destinationTeamId: number;
  expectedTeamId: number | null;
  label: string;
  description: string;
  done?: () => void;
  width?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <VStack gap={3}>
      <HStack gap={3}>
        <Button
          label={label}
          width={width}
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        />
      </HStack>
      {open && (
        <OwnershipDocumentationConfirmation
          key={`${repositoryId}:${destinationTeamId}`}
          repositoryId={repositoryId}
          destinationTeamId={destinationTeamId}
          expectedTeamId={expectedTeamId}
          label={label}
          description={description}
          done={() => {
            setOpen(false);
            done?.();
          }}
          cancel={() => setOpen(false)}
        />
      )}
    </VStack>
  );
}

function OwnershipDocumentationConfirmation({
  repositoryId,
  destinationTeamId,
  expectedTeamId,
  label,
  description,
  done,
  cancel,
}: {
  repositoryId: number;
  destinationTeamId: number;
  expectedTeamId: number | null;
  label: string;
  description: string;
  done: () => void;
  cancel: () => void;
}) {
  const scope = useScope();
  const refresh = usePolicyRefresh();
  const [stale, setStale] = useState(false);
  const query = useQuery({
    queryKey: [
      "ownership-documentation",
      repositoryId,
      destinationTeamId,
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<OwnershipPreview>(
        scope.path(
          `/api/repository-ownership/${repositoryId}/preview?destination_team_id=${destinationTeamId}`,
        ),
        signal,
      ),
    refetchOnWindowFocus: false,
  });
  const save = useMutation({
    mutationFn: () =>
      write(scope.path(`/api/repository-ownership/${repositoryId}`), "PUT", {
        team_id: destinationTeamId,
        expected_team_id: expectedTeamId,
        expected_documentation_revision: query.data!.revision,
        reason: label,
      } satisfies components["schemas"]["RepositoryAssignment"]),
    onSuccess: async () => {
      await refresh();
      done();
    },
    onError: (error) => {
      if (error instanceof APIError && error.status === 409) setStale(true);
    },
  });
  const ownershipChanged =
    query.data !== undefined && query.data.previous_team_id !== expectedTeamId;
  return (
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        if (query.data && !stale && !ownershipChanged && !query.isFetching)
          save.mutate();
      }}
    >
      <VStack gap={3}>
        <Text as="p">{description}</Text>
        <Freshness query={query} interval={false} />
        {ownershipChanged && (
          <Banner
            status="warning"
            title="The repository has moved"
            description="Close this preview and reopen the repository from its current owning team before moving it again."
          />
        )}
        {query.data && (
          <>
            <MetadataList columns="single" label={{ position: "top" }}>
              <MetadataListItem label="Documentation reviews now">
                <EffectiveMode mode={query.data.before} />
              </MetadataListItem>
              <MetadataListItem label="After assignment">
                <EffectiveMode mode={query.data.after} />
              </MetadataListItem>
            </MetadataList>
            <Text as="p">
              {query.data.account_policy_changed
                ? "The destination team's model and account policy will apply to new requests."
                : "The model and account policy stays the same."}
            </Text>
          </>
        )}
        {save.isError && (
          <Banner
            status={stale ? "warning" : "error"}
            title={
              stale
                ? "Ownership or policy changed"
                : "Could not move the repository"
            }
            description={save.error.message}
          />
        )}
        <HStack gap={3} wrap="wrap">
          {stale && (
            <Button
              label="Refresh assignment preview"
              isDisabled={query.isFetching || save.isPending}
              onClick={async () => {
                const result = await query.refetch();
                if (result.isSuccess) {
                  setStale(false);
                  save.reset();
                }
              }}
            />
          )}
          <Button
            label={label}
            type="submit"
            variant="primary"
            isDisabled={
              !query.data ||
              query.isFetching ||
              query.isError ||
              stale ||
              ownershipChanged ||
              save.isPending
            }
            isLoading={save.isPending}
          />
          <Button label="Cancel" isDisabled={save.isPending} onClick={cancel} />
        </HStack>
      </VStack>
    </Form>
  );
}

export function ApprovalDocumentationSummary({ teamId }: { teamId: number }) {
  const scope = useScope();
  const query = useQuery({
    queryKey: ["team-documentation", teamId, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPolicy>(
        scope.path(`/api/teams/${teamId}/documentation?limit=1`),
        signal,
      ),
  });
  return (
    <VStack gap={2}>
      <Freshness query={query} interval={false} />
      {query.data && (
        <Text as="p">
          Destination team documentation default:{" "}
          {documentationModeLabel(query.data.default_mode)}
          {query.data.deployment_enabled
            ? "."
            : ". Documentation reviews are currently disabled for the deployment."}{" "}
          Existing repository overrides are preserved.
        </Text>
      )}
    </VStack>
  );
}

/** Where documentation reviews stand across the deployment.
 *
 *  The setting itself lives in three places by design: a deployment switch, a
 *  team default, and a repository override. Until now each was reached from a
 *  different corner of the console, so the one question an operator actually
 *  asks — which repositories run documentation reviews, and why — had no
 *  screen. This is that screen; it changes nothing on its own and hands each
 *  repository to the editor that already owns it. */
export function DocumentationOverviewPage() {
  const scope = useScope();
  /* A table puts the mode in a second column, and on a phone a second column
     is off the screen: the page would show a list of repository names and
     hide the one thing it exists to say. Narrow screens get the same fields
     stacked. */
  const isNarrow = useMediaQuery("(max-width: 768px)");
  const { params, update } = useFilters();
  const search = params.get("search") ?? "";
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const { draft, setDraft, flush } = useLiveSearch(search, (value) =>
    update({ search: value }, { replace: true }),
  );
  useEffect(() => {
    document.title = "Review Agent · Documentation reviews";
  }, []);
  const queryParams = new URLSearchParams({
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
  const data = query.data;
  const deploymentEnabled = data?.items[0]?.documentation.deployment_enabled;
  const columns: TableColumn<RepositoryPage["items"][number]>[] = [
    {
      key: "repository",
      header: "Repository",
      width: proportional(2, { minWidth: 240 }),
      renderCell: (repo) => (
        <VStack gap={1}>
          <Text weight="medium">{repo.repository}</Text>
          <Text type="supporting">
            {repo.team_id ? (
              <Link
                to={`/teams/${repo.team_id}?team_id=${repo.team_id}&tab=documentation`}
              >
                {repo.team_name}
              </Link>
            ) : (
              "No team"
            )}
          </Text>
        </VStack>
      ),
    },
    {
      key: "mode",
      header: "Documentation reviews",
      width: proportional(1, { minWidth: 220 }),
      renderCell: (repo) => <EffectiveMode mode={repo.documentation} />,
    },
    {
      key: "actions",
      header: "Settings",
      width: pixel(150),
      renderCell: (repo) => (
        <Link
          to={`/repositories?documentation_repository=${repo.repository_id}`}
        >
          Open settings
        </Link>
      ),
    },
  ];
  return (
    <>
      <VStack gap={3}>
        <Heading level={1}>Documentation reviews</Heading>
        <Text as="p" color="secondary">
          Which repositories check that documentation matches the change, and
          where each mode comes from.
        </Text>
        <ModeHelp />
      </VStack>
      {/* The deployment switch overrides every team and repository, so it is
          settled before the list that it governs. */}
      {deploymentEnabled === false ? (
        <DeploymentPaused enabled={false} />
      ) : deploymentEnabled === true ? (
        <HStack gap={3} wrap="wrap" vAlign="center">
          <HStack gap={2} vAlign="center">
            <StatusDot
              variant="success"
              label="Enabled for this deployment"
              aria-hidden="true"
            />
            <Text>Enabled for this deployment</Text>
          </HStack>
          {scope.current.role === "owner" ? (
            <Link to="/settings">Change in Settings</Link>
          ) : null}
        </HStack>
      ) : null}
      <HStack gap={4} wrap="wrap" vAlign="end">
        <TextInput
          label="Find a repository"
          id="documentation-search"
          startIcon="search"
          hasClear
          width={280}
          placeholder="owner/repository"
          value={draft}
          onChange={(value) => setDraft(value)}
          onEnter={flush}
        />
        <Link to="/teams">Team defaults</Link>
      </HStack>
      <Freshness query={query} />
      {query.isPending && <Loading rows={6} label="Loading repositories" />}
      {data &&
        (data.items.length ? (
          isNarrow ? (
            <VStack gap={0} aria-label="Documentation review modes">
              {data.items.map((repo, index) => (
                <AstryxSection
                  key={repo.repository_id}
                  variant="transparent"
                  padding={0}
                  paddingBlock={4}
                  dividers={index === 0 ? undefined : ["top"]}
                >
                  <VStack gap={2}>
                    <Text weight="medium">{repo.repository}</Text>
                    <EffectiveMode mode={repo.documentation} />
                    <HStack gap={4} wrap="wrap" vAlign="center">
                      {repo.team_id ? (
                        <Link
                          to={`/teams/${repo.team_id}?team_id=${repo.team_id}&tab=documentation`}
                        >
                          {repo.team_name}
                        </Link>
                      ) : (
                        <Text type="supporting">No team</Text>
                      )}
                      <Link
                        to={`/repositories?documentation_repository=${repo.repository_id}`}
                      >
                        Open settings
                      </Link>
                    </HStack>
                  </VStack>
                </AstryxSection>
              ))}
            </VStack>
          ) : (
            <Table
              aria-label="Documentation review modes"
              data={data.items}
              columns={columns}
              idKey="repository_id"
              density="balanced"
              dividers="rows"
              hasHover
              verticalAlign="top"
            />
          )
        ) : (
          <Empty
            title={search ? "No matching repositories" : "No repositories yet"}
          >
            {search
              ? `No repository matches “${search}”.`
              : "Repositories appear here once Review Agent has registered them."}
          </Empty>
        ))}
      {data && (offset > 0 || data.has_more) && (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label="Previous"
            variant="secondary"
            isDisabled={offset === 0}
            onClick={() => update({ offset: String(Math.max(0, offset - 50)) })}
          />
          <Text>Page {Math.floor(offset / 50) + 1}</Text>
          <Button
            label="Next"
            variant="secondary"
            isDisabled={!data.has_more || offset >= 10000}
            onClick={() => update({ offset: String(offset + 50) })}
          />
        </HStack>
      )}
    </>
  );
}
