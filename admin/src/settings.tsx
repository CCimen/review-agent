import { Code } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { InputHTMLAttributes } from "react";
import { Form } from "./ui";
// Settings layout adapted from Astryx's Settings Form template.
// Copyright (c) Meta Platforms, Inc. and affiliates.
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Divider } from "@astryxdesign/core/Divider";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Heading, Text } from "@astryxdesign/core/Text";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { FormEvent, ReactNode } from "react";
import { useEffect, useState } from "react";
import { SettingsTabs } from "./accounts";
import { read, write } from "./api";
import type { components } from "./api.generated";
import { DeploymentLink } from "./deployment";
import { EmailDelivery } from "./email";
import { Providers } from "./providers";
import { Freshness, time } from "./ui";

type Settings = components["schemas"]["DeploymentSettings"];
type Page = components["schemas"]["DeploymentSettingsPage"];
type Revision = components["schemas"]["SettingsRevision"];
const numericFields = [
  [
    "active_job_limit",
    "Maximum active reviews",
    "Maximum queued and running reviews.",
  ],
  [
    "capacity_retry_seconds",
    "Queue recheck delay (seconds)",
    "Delay before checking a full queue again.",
  ],
  [
    "worker_concurrency",
    "Reviews per worker",
    "Simultaneous reviews per worker process.",
  ],
  [
    "job_max_attempts",
    "Maximum review attempts",
    "Attempt budget for newly admitted reviews.",
  ],
  [
    "job_lease_seconds",
    "Worker claim duration (seconds)",
    "Ownership duration renewed by the worker.",
  ],
  [
    "job_heartbeat_seconds",
    "Heartbeat interval (seconds)",
    "Must be less than half the lease duration.",
  ],
  [
    "hermes_timeout_seconds",
    "Review timeout (seconds)",
    "Maximum duration of one Hermes request.",
  ],
  [
    "publish_max_bytes",
    "Publication size limit (bytes)",
    "Between 1,000 and 65,000 bytes.",
  ],
  [
    "publication_max_attempts",
    "Maximum publication attempts",
    "Delivery attempt budget for new publications.",
  ],
] as const;

const advancedGroups = [
  {
    title: "Queue scheduling and recovery",
    fields: [
      [
        "job_priority",
        "Default review priority",
        "Higher priorities run first. Applies to new reviews.",
      ],
      [
        "job_priority_aging_seconds",
        "Priority aging interval (seconds)",
        "Helps older queued reviews progress alongside newer work.",
      ],
      [
        "job_retry_seconds",
        "Review retry delay (seconds)",
        "Wait after a retryable review failure.",
      ],
      [
        "job_poll_seconds",
        "Review queue poll interval (seconds)",
        "How often idle workers look for reviews.",
      ],
      [
        "job_recovery_seconds",
        "Recovery interval (seconds)",
        "How often workers check expired claims.",
      ],
      [
        "job_recovery_batch_size",
        "Recovery batch size",
        "Maximum expired claims handled in one recovery pass.",
      ],
      [
        "admission_max_age_seconds",
        "Maximum admission age (seconds)",
        "How long an incoming review request may wait for admission.",
      ],
    ],
  },
  {
    title: "Publication delivery",
    fields: [
      [
        "publication_lease_seconds",
        "Publication claim duration (seconds)",
        "Ownership duration renewed by the publisher.",
      ],
      [
        "publication_heartbeat_seconds",
        "Publication heartbeat interval (seconds)",
        "Must be less than half the publication claim duration.",
      ],
      [
        "publication_retry_seconds",
        "Publication retry delay (seconds)",
        "Wait before retrying a failed delivery.",
      ],
      [
        "publication_poll_seconds",
        "Publication queue poll interval (seconds)",
        "How often the publisher looks for work.",
      ],
    ],
  },
  {
    title: "Incoming requests and GitHub capacity",
    fields: [
      [
        "github_app_max_body_bytes",
        "Maximum webhook size (bytes)",
        "Reject larger incoming payloads. Maximum 2,097,152 bytes.",
      ],
      [
        "admission_max_concurrent_requests",
        "Concurrent webhook requests",
        "Maximum incoming requests handled at once.",
      ],
      [
        "admission_request_timeout_seconds",
        "Webhook request timeout (seconds)",
        "Maximum time allowed for an incoming request.",
      ],
      [
        "github_gateway_max_concurrent_requests",
        "Concurrent GitHub gateway requests",
        "Maximum requests handled by each gateway process.",
      ],
    ],
  },
] as const;
type NumericField =
  | (typeof numericFields)[number]
  | (typeof advancedGroups)[number]["fields"][number];
type ModelPage = components["schemas"]["ProviderModelPage"];

export function SettingsPage() {
  const query = useQuery({
    queryKey: ["deployment-settings"],
    refetchInterval: false,
    refetchOnWindowFocus: false,
    queryFn: ({ signal }) => read<Page>("/api/settings", signal),
  });
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Settings</Heading>
          <Text as="p">
            Manage provider connections, review behavior, and user access.
          </Text>
        </VStack>
      </HStack>
      <SettingsTabs />
      <Providers />
      <Collapsible trigger="Email delivery (SMTP)" defaultIsOpen={false}>
        <EmailDelivery />
      </Collapsible>
      <Freshness query={query} interval={false} />
      {query.data && <SettingsEditor data={query.data} />}
    </>
  );
}

function SettingsGroup({
  title,
  description,
  disabled,
  children,
}: {
  title: string;
  description: string;
  disabled: boolean;
  children: ReactNode;
}) {
  return (
    <Grid columns={{ minWidth: 280, max: 2 }} gap={6}>
      <VStack gap={2}>
        <Heading level={3}>{title}</Heading>
        <Text type="supporting">{description}</Text>
      </VStack>
      <fieldset aria-label={title} disabled={disabled}>
        <VStack gap={5}>{children}</VStack>
      </fieldset>
    </Grid>
  );
}

function SettingsEditor({ data }: { data: Page }) {
  const client = useQueryClient();
  const [original, setOriginal] = useState(data);
  const [draft, setDraft] = useState<Settings>(data.settings);
  const [restoredRevision, setRestoredRevision] = useState<number | null>(
    null,
  );
  const [before, setBefore] = useState<number | null>(null);
  const models = useQuery({
    queryKey: ["provider-models"],
    queryFn: ({ signal }) =>
      read<ModelPage>("/api/providers/models", signal),
    refetchInterval: false,
    staleTime: 300_000,
  });
  const history = useQuery({
    refetchInterval: false,
    refetchOnWindowFocus: false,
    queryKey: ["deployment-settings-history", before],
    queryFn: ({ signal }) =>
      read<Page>(
        `/api/settings${before ? `?before_id=${before}` : ""}`,
        signal,
      ),
  });
  const save = useMutation({
    mutationFn: () =>
      write<Revision>("/api/settings", "PUT", {
        expected_revision: original.revision,
        settings: draft,
        reason:
          restoredRevision === null
            ? "Deployment settings updated"
            : `Settings restored from revision ${restoredRevision}`,
      }),
    onSuccess: async (revision) => {
      setOriginal({
        ...data,
        settings: revision.settings,
        revision: revision.id,
      });
      setDraft(revision.settings);
      setRestoredRevision(null);
      await client.invalidateQueries({ queryKey: ["deployment-settings"] });
      await client.invalidateQueries({
        queryKey: ["deployment-settings-history"],
      });
    },
  });
  const dirty = JSON.stringify(draft) !== JSON.stringify(original.settings);
  useEffect(() => {
    if (!dirty && data.revision > original.revision) {
      setOriginal(data);
      setDraft(data.settings);
      setRestoredRevision(null);
    }
  }, [data, original.revision, dirty]);
  function numericInputs(fields: readonly NumericField[]) {
    return fields.map(([key, label, hint]) => (
      <VStack gap={2} key={key}>
        <NumberInput
          isIntegerOnly
          label={label}
          id={`setting-${key}`}
          isRequired={true}
          min={
            key === "publish_max_bytes"
              ? 1000
              : key === "job_priority"
                ? 0
                : 1
          }
          max={
            key === "publish_max_bytes"
              ? 65000
              : key === "github_app_max_body_bytes"
                ? 2097152
                : key === "job_priority"
                  ? 2147483647
                  : undefined
          }
          step={1}
          value={draft[key]}
          onChange={(value) => setDraft({ ...draft, [key]: value })}
          description={hint}
          {...({
            required: true,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />
      </VStack>
    ));
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }
  return (
    <>
      <Form onSubmit={submit}>
        <VStack gap={6}>
          <HStack justify="between" align="center" gap={3} wrap="wrap">
            <Heading level={2}>Review settings</Heading>
            <Text type="supporting">
              {original.revision
                ? `Revision ${original.revision}`
                : "Environment defaults"}
            </Text>
          </HStack>
          <Banner
            status="info"
            title="When changes take effect"
            description="Model changes apply to new requests. Queued reviews keep their selected model. Other settings take effect after the relevant services restart."
          />
          <SettingsGroup
            title="Review model"
            description="Choose the provider, model, and reasoning effort for new requests."
            disabled={save.isPending}
          >
            <Selector
              label="Provider"
              value={draft.model_provider}
              isDisabled={save.isPending}
              options={[
                { value: "openai-codex", label: "OpenAI Codex" },
                { value: "anthropic", label: "Anthropic" },
              ]}
              onChange={(value) =>
                setDraft({
                  ...draft,
                  model_provider: value as Settings["model_provider"],
                })
              }
            />

            <TextInput
              label={"Model ID"}
              id="setting-model"
              isRequired={true}
              value={draft.model}
              onChange={(value) => setDraft({ ...draft, model: value })}
              description="Suggestions come from Hermes. You can also enter a supported model ID."
              {...({
                required: true,
                maxLength: 200,
                list: "review-model-options",
              } satisfies InputHTMLAttributes<HTMLInputElement>)}
            />

            <datalist id="review-model-options">
              {models.data?.items
                .filter((item) => item.provider === draft.model_provider)
                .map((item) => (
                  <option key={item.model} value={item.model} />
                ))}
            </datalist>
            <Selector
              label="Reasoning effort"
              value={draft.reasoning_effort}
              isDisabled={save.isPending}
              options={(
                [
                  "none",
                  "minimal",
                  "low",
                  "medium",
                  "high",
                  "xhigh",
                  "max",
                  "ultra",
                ] as const
              ).map((value) => ({
                value,
                label:
                  value === "xhigh"
                    ? "Extra high"
                    : value === "max"
                      ? "Maximum"
                      : value.charAt(0).toUpperCase() + value.slice(1),
              }))}
              onChange={(value) =>
                setDraft({
                  ...draft,
                  reasoning_effort: value as Settings["reasoning_effort"],
                })
              }
            />
          </SettingsGroup>
          <Divider />
          <SettingsGroup
            title="Workload and delivery"
            description="Control review capacity, timeouts, and publication attempts."
            disabled={save.isPending}
          >
            {numericInputs(numericFields)}
          </SettingsGroup>
          <Divider />
          <SettingsGroup
            title="Feedback and repository context"
            description="Choose which feedback and repository context reviews can use."
            disabled={save.isPending}
          >
            <CheckboxInput
              label="Include feedback instructions in reviews"
              value={draft.feedback_enabled}
              isDisabled={save.isPending}
              onChange={(value) =>
                setDraft({ ...draft, feedback_enabled: value })
              }
            />
            <CheckboxInput
              label="Include repository code relationships"
              value={draft.code_graph_enabled}
              isDisabled={save.isPending}
              onChange={(value) =>
                setDraft({ ...draft, code_graph_enabled: value })
              }
            />
            <Selector
              label="Code graph embeddings"
              value={draft.code_graph_embeddings}
              isDisabled={save.isPending}
              description="Requires the code graph service; OpenAI embeddings also require its gateway API key."
              options={[
                { value: "none", label: "None" },
                { value: "openai", label: "OpenAI" },
              ]}
              onChange={(value) =>
                setDraft({
                  ...draft,
                  code_graph_embeddings:
                    value as Settings["code_graph_embeddings"],
                })
              }
            />
          </SettingsGroup>
          <Divider />
          <Collapsible
            defaultIsOpen={false}
            trigger={
              <HStack gap={3} wrap="wrap" vAlign="center">
                Advanced operational settings
              </HStack>
            }
          >
            <VStack gap={4}>
              <VStack gap={6}>
                <Text type="supporting">
                  These controls apply after the relevant services restart.
                  Environment values provide the initial defaults.
                </Text>
                {advancedGroups.map((group) => (
                  <SettingsGroup
                    key={group.title}
                    title={group.title}
                    description="Saved deployment defaults for this service."
                    disabled={save.isPending}
                  >
                    {numericInputs(group.fields)}
                  </SettingsGroup>
                ))}
              </VStack>
            </VStack>
          </Collapsible>

          {save.error && (
            <Banner
              status="error"
              title="Could not save settings"
              description={save.error.message}
              endContent={
                <Button
                  label="Reload saved settings and discard edits"
                  onClick={() => {
                    setOriginal(data);
                    setDraft(data.settings);
                    setRestoredRevision(null);
                    void client.invalidateQueries({
                      queryKey: ["deployment-settings"],
                    });
                  }}
                />
              }
            />
          )}
          <HStack gap={3} wrap="wrap" align="center">
            <Button
              type="submit"
              variant="primary"
              label={save.isPending ? "Saving…" : "Save settings"}
              isDisabled={!dirty}
              isLoading={save.isPending}
            />
            <Button
              label="Discard edits"
              isDisabled={!dirty || save.isPending}
              onClick={() => {
                setDraft(original.settings);
                setRestoredRevision(null);
              }}
            />
            <Text type="supporting" role="status">
              {dirty ? "Changes ready to save" : "No unsaved changes"}
            </Text>
          </HStack>
        </VStack>
      </Form>
      <VStack gap={4} as="section">
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Heading level={2}>Settings loaded by services</Heading>
        </HStack>
        <VStack gap={4}>
          <Text as="p" color="secondary">
            Last 50 service starts. These records show which revision was
            loaded at startup; use Health to check current workers. Restart
            services through your deployment platform.
          </Text>
          <DeploymentLink />
          <VStack gap={0}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHeaderCell>Service</TableHeaderCell>
                  <TableHeaderCell>Instance</TableHeaderCell>
                  <TableHeaderCell>Revision</TableHeaderCell>
                  <TableHeaderCell>Policy status</TableHeaderCell>
                  <TableHeaderCell>Loaded</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.startup_loads.map((row, index) => (
                  <TableRow key={index}>
                    <TableCell>{row.service}</TableCell>
                    <TableCell>
                      <Code>{row.hostname}</Code>
                    </TableCell>
                    <TableCell>{row.revision ?? "Environment"}</TableCell>
                    <TableCell>
                      {(row.revision ?? 0) === data.revision
                        ? "Current at startup"
                        : "Older settings loaded"}
                    </TableCell>
                    <TableCell>{time(row.loaded_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </VStack>
          {!data.startup_loads.length && (
            <Text as="p" color="secondary">
              No startup observations yet.
            </Text>
          )}
        </VStack>
      </VStack>
      <VStack gap={4} as="section">
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Heading level={2}>Settings history</Heading>
        </HStack>
        <VStack gap={4}>
          <Freshness query={history} interval={false} />
          {history.data?.history.map((revision) => (
            <HStack gap={3} wrap="wrap" vAlign="center" key={revision.id}>
              <VStack gap={3}>
                <strong>Revision {revision.id}</strong>
                <Text as="p">{revision.reason}</Text>
                <Text color="secondary">
                  {time(revision.created_at)} · {revision.actor}
                </Text>
              </VStack>
              <Button
                label={"Restore to editor"}
                variant="secondary"
                type="button"
                isDisabled={revision.id === data.revision || save.isPending}
                onClick={() => {
                  setDraft(revision.settings);
                  setRestoredRevision(revision.id);
                  window.scrollTo({ top: 0, behavior: "instant" });
                }}
              />
            </HStack>
          ))}
          {history.data?.history.length === 0 && (
            <Text as="p" color="secondary">
              Saving a policy creates its first revision.
            </Text>
          )}
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <Button
              label={"Newest"}
              variant="secondary"
              type="submit"
              isDisabled={before === null}
              onClick={() => setBefore(null)}
            />
            <Button
              label={"Older"}
              variant="secondary"
              type="submit"
              isDisabled={!history.data?.next_before_id}
              onClick={() =>
                setBefore(history.data?.next_before_id ?? null)
              }
            />
          </HStack>
        </VStack>
      </VStack>
    </>
  );
}
