import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Code } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import { Selector } from "@astryxdesign/core/Selector";
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
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { InputHTMLAttributes } from "react";
import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import type { TeamPage } from "./api";
import { read, write } from "./api";
import type { components } from "./api.generated";
import { ConnectionQuota } from "./modelQuota";
import { ScopedLink as Link, useScope } from "./scope";
import { ConfirmAction } from "./teams";
import { Copy, Empty, Form, Freshness, time } from "./ui";

type Connection = components["schemas"]["ModelConnection"];
type ConnectionPage = components["schemas"]["ConnectionPage"];
type ModelChoice = components["schemas"]["ModelChoiceInput"];
type ModelLogin = components["schemas"]["ModelLogin"];
type Runtime = components["schemas"]["ConnectionRuntime"];

const providerNames = {
  "openai-codex": "OpenAI Codex",
  anthropic: "Anthropic",
};
const stateNames: Record<Connection["state"], string> = {
  enabled: "Enabled",
  disabled: "Paused",
  authenticating: "Login in progress",
  needs_attention: "Needs attention",
  retired: "Retired",
};
const effortChoices = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
  "ultra",
];

function useConnectionRefresh() {
  const client = useQueryClient();
  return () =>
    Promise.all(
      [
        "model-connections",
        "team-model-policy",
        "model-login",
        "model-runtime",
        "model-quota",
        "audit",
      ].map((key) => client.invalidateQueries({ queryKey: [key] })),
    );
}

function ConnectionEditor({
  connection,
  done,
}: {
  connection?: Connection;
  done?: () => void;
}) {
  const scope = useScope();
  const navigate = useNavigate();
  const refresh = useConnectionRefresh();
  const [name, setName] = useState(connection?.name ?? "");
  const [maxConcurrency, setMaxConcurrency] = useState(
    connection?.max_concurrency ?? 4,
  );
  const [runtimeKey, setRuntimeKey] = useState("");
  const [teamId, setTeamId] = useState(scope.teamId ?? "");
  const [teamSearch, setTeamSearch] = useState("");
  const [choices, setChoices] = useState<ModelChoice[]>(
    connection?.allowed_routes ?? [],
  );
  const runtimes = useQuery({
    queryKey: ["model-connections", "runtimes", "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<string[]>("/api/model-connections/runtimes", signal),
    enabled: !connection,
  });
  const teams = useQuery({
    queryKey: ["teams", "model-owner", teamSearch, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPage>(
        `/api/teams?limit=20&search=${encodeURIComponent(teamSearch)}`,
        signal,
      ),
    enabled: !connection && teamSearch.trim().length >= 2,
  });
  const save = useMutation({
    mutationFn: () =>
      connection
        ? write<Connection>(
            scope.path(`/api/model-connections/${connection.id}`),
            "PATCH",
            {
              name,
              allowed_routes: choices,
              max_concurrency: maxConcurrency,
              expected_revision: connection.revision,
              reason: "Model connection updated",
            } satisfies components["schemas"]["ConnectionUpdate"],
          )
        : write<Connection>("/api/model-connections", "POST", {
            name,
            runtime_key: runtimeKey,
            team_id: teamId ? Number(teamId) : null,
            allowed_routes: choices,
            max_concurrency: maxConcurrency,
            reason: "Model connection created",
          } satisfies components["schemas"]["ConnectionCreate"]),
    onSuccess: async (value) => {
      await refresh();
      done?.();
      if (!connection)
        navigate(
          `/model-connections/${value.id}${value.team_id ? `?team_id=${value.team_id}` : ""}`,
        );
    },
  });
  function updateChoice(index: number, value: Partial<ModelChoice>) {
    setChoices((items) =>
      items.map((item, at) =>
        at === index ? { ...item, ...value } : item,
      ),
    );
  }
  return (
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <TextInput
        label={"Connection name"}
        isRequired={true}
        value={name}
        onChange={(value) => setName(value)}
        {...({
          required: true,
          maxLength: 80,
        } satisfies InputHTMLAttributes<HTMLInputElement>)}
      />

      <NumberInput
        isIntegerOnly
        label={"Maximum concurrent reviews"}
        isRequired={true}
        min={1}
        max={2147483647}
        step={1}
        value={maxConcurrency}
        onChange={(value) => setMaxConcurrency(value)}
        {...({
          required: true,
        } satisfies InputHTMLAttributes<HTMLInputElement>)}
      />

      <Text as="p" color="secondary">
        Shared by all workers using this connection. Lowering the limit lets
        reviews already claimed finish.
      </Text>
      {!connection ? (
        <>
          <Freshness query={runtimes} />
          <Selector
            label={"Provisioned runtime"}
            options={[
              { value: "", label: "Choose a runtime" },
              runtimes.data
                ?.filter((key) => key !== "shared")
                .map((key) => ({ value: key, label: key })),
            ]
              .flat()
              .filter((option) => option != null)}
            isRequired={true}
            value={runtimeKey}
            onChange={(value) => setRuntimeKey(value)}
          />
          <Text as="p" color="secondary">
            A platform operator provisions each runtime and its private
            credential storage first.
          </Text>
          {runtimes.data &&
          !runtimes.data.some((key) => key !== "shared") ? (
            <Text as="p">
              No additional runtime is configured.{" "}
              <AstryxLink
                href="https://ccimen.github.io/review-agent/admin-panel#managed-model-connections"
                target="_blank"
                rel="noreferrer"
              >
                Open setup instructions
              </AstryxLink>
              .
            </Text>
          ) : null}

          <TextInput
            label={"Find an owning team"}
            placeholder="Type at least two characters"
            value={teamSearch}
            onChange={(value) => setTeamSearch(value)}
            {...({
              maxLength: 80,
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
          />

          {teamSearch.trim().length >= 2 ? (
            <Freshness query={teams} />
          ) : null}
          <Selector
            label={"Connection owner"}
            options={[
              { value: "", label: "Platform · Shared connection" },
              teamId &&
              !teams.data?.items.some((team) => String(team.id) === teamId)
                ? {
                    value: teamId,
                    label: String(
                      String(scope.team?.id) === teamId
                        ? scope.team?.name
                        : `Team ${teamId}`,
                    ),
                  }
                : null,
              teams.data?.items.map((team) => ({
                value: String(team.id),
                label: team.name,
              })),
            ]
              .flat()
              .filter((option) => option != null)}
            value={teamId}
            onChange={(value) => setTeamId(value)}
          />
          {teams.data?.next_after_id ? (
            <Text as="p" color="secondary">
              Refine the team name to find more results.
            </Text>
          ) : null}
        </>
      ) : null}
      <Heading level={3}>Allowed team choices</Heading>
      <Text as="p">
        Teams may inherit deployment defaults or choose one of these models
        and reasoning levels.
      </Text>
      <VStack gap={4}>
        {choices.map((choice, index) => (
          <fieldset key={index}>
            <VStack gap={4}>
              <legend>Model {index + 1}</legend>
              <Grid
                gap={4}
                columns={{ minWidth: 240, max: 4, repeat: "fit" }}
              >
                <Selector
                  label={"Provider"}
                  options={[
                    { value: "openai-codex", label: "OpenAI Codex" },
                    { value: "anthropic", label: "Anthropic" },
                  ]}
                  value={choice.provider}
                  onChange={(value) =>
                    updateChoice(index, {
                      provider: value as ModelChoice["provider"],
                    })
                  }
                />

                <TextInput
                  label={"Model ID"}
                  isRequired={true}
                  value={choice.model}
                  onChange={(value) =>
                    updateChoice(index, { model: value })
                  }
                  {...({
                    required: true,
                    maxLength: 200,
                  } satisfies InputHTMLAttributes<HTMLInputElement>)}
                />
              </Grid>
              <VStack gap={4}>
                {effortChoices.map((effort) => (
                  <VStack gap={2} key={effort}>
                    <CheckboxInput
                      label={effort}
                      value={choice.reasoning_efforts.includes(effort)}
                      onChange={(value) =>
                        updateChoice(index, {
                          reasoning_efforts: value
                            ? [...choice.reasoning_efforts, effort]
                            : choice.reasoning_efforts.filter(
                                (value) => value !== effort,
                              ),
                        })
                      }
                    />
                  </VStack>
                ))}
              </VStack>
              <Button
                label={"Remove model"}
                variant="primary"
                type="button"
                onClick={() =>
                  setChoices((items) =>
                    items.filter((_item, at) => at !== index),
                  )
                }
              />
            </VStack>
          </fieldset>
        ))}
      </VStack>
      <Button
        label={"Add a model choice"}
        variant="secondary"
        type="button"
        isDisabled={choices.length >= 50}
        onClick={() =>
          setChoices((items) => [
            ...items,
            {
              provider: "openai-codex",
              model: "",
              reasoning_efforts: ["high"],
            },
          ])
        }
      />

      {save.isError ? (
        <Text as="p" role="alert">
          {save.error.message}
        </Text>
      ) : null}
      {save.isSuccess ? (
        <Text as="p" role="status">
          Connection saved.
        </Text>
      ) : null}
      <Button
        label={String(
          save.isPending
            ? "Saving…"
            : connection
              ? "Save connection"
              : "Add connection",
        )}
        variant="primary"
        type="submit"
        isDisabled={
          save.isPending ||
          (!connection && !runtimeKey) ||
          choices.some((choice) => choice.reasoning_efforts.length === 0)
        }
      />
    </Form>
  );
}

export function ModelConnectionsPage() {
  const scope = useScope();
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const after = params.get("after_id") ?? "0";
  const query = useQuery({
    queryKey: ["model-connections", "list", after, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<ConnectionPage>(
        scope.path(
          `/api/model-connections?after_id=${encodeURIComponent(after)}`,
        ),
        signal,
      ),
  });
  useEffect(() => {
    document.title = "Review Agent · Model connections";
  }, []);
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Model connections</Heading>
          <Text as="p">
            {scope.team
              ? `Provider accounts available to ${scope.team.name}.`
              : "Shared and team-owned provider accounts."}
          </Text>
        </VStack>
        {scope.current.role === "owner" ? (
          <Button
            label={String(adding ? "Close form" : "Add connection")}
            variant={adding ? "secondary" : "primary"}
            onClick={() => setAdding(!adding)}
            aria-expanded={adding}
          />
        ) : null}
      </HStack>
      {adding ? (
        <VStack gap={4} as="section">
          <Heading level={2}>Add a managed connection</Heading>
          <ConnectionEditor done={() => setAdding(false)} />
        </VStack>
      ) : null}
      <Freshness query={query} />
      {query.data?.items.length ? (
        <VStack
          gap={4}
          role="region"
          tabIndex={0}
          aria-label="Model connections"
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Connection</TableHeaderCell>
                <TableHeaderCell>Owner</TableHeaderCell>
                <TableHeaderCell>Status</TableHeaderCell>
                <TableHeaderCell>Recorded accounts</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.map((connection) => (
                <TableRow key={connection.id}>
                  <TableHeaderCell scope="row">
                    <Link to={`/model-connections/${connection.id}`}>
                      {connection.name}
                    </Link>
                  </TableHeaderCell>
                  <TableCell>
                    {connection.team_name ?? "Platform · Shared"}
                  </TableCell>
                  <TableCell>
                    <Text>{stateNames[connection.state]}</Text>
                  </TableCell>
                  <TableCell>
                    {connection.accounts.some((account) => account.verified)
                      ? connection.accounts
                          .filter((account) => account.verified)
                          .map((account) => providerNames[account.provider])
                          .join(", ")
                      : "No identity recorded"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </VStack>
      ) : query.data ? (
        <Empty title="No model connections in this view">
          An administrator can assign a shared or dedicated connection to
          your team.
        </Empty>
      ) : null}
      {after !== "0" || query.data?.next_after_id ? (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"First page"}
            variant="secondary"
            type="submit"
            isDisabled={after === "0"}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("after_id");
              setParams(next);
            }}
          />
          <Button
            label={"Next connections"}
            variant="secondary"
            type="submit"
            isDisabled={!query.data?.next_after_id}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("after_id", String(query.data?.next_after_id));
              setParams(next);
            }}
          />
        </HStack>
      ) : null}
    </>
  );
}

function ConnectionLogin({
  connection,
  configured,
}: {
  connection: Connection;
  configured: boolean;
}) {
  const scope = useScope();
  const client = useQueryClient();
  const refresh = useConnectionRefresh();
  const [challenge, setChallenge] = useState<ModelLogin | null>(null);
  const operationId = challenge?.id ?? connection.active_login_id;
  const key = [
    "model-login",
    connection.id,
    operationId,
    "scoped",
    scope.key,
  ];
  const path = scope.path(`/api/model-connections/${connection.id}/login`);
  const session = useQuery({
    queryKey: key,
    queryFn: ({ signal }) =>
      read<ModelLogin>(
        scope.path(
          `/api/model-connections/${connection.id}/login/${operationId}`,
        ),
        signal,
      ),
    enabled: !!operationId,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const current = session.data ?? challenge;
  const start = useMutation({
    mutationFn: () =>
      write<ModelLogin>(path, "POST", {
        expected_revision: connection.revision,
        reason: "Provider login started",
      } satisfies components["schemas"]["ConnectionChange"]),
    onSuccess: (value) => {
      setChallenge(value);
      client.setQueryData(
        ["model-login", connection.id, value.id, "scoped", scope.key],
        value,
      );
    },
    onSettled: () => refresh(),
  });
  const advance = useMutation({
    mutationFn: (action: "poll" | "cancel") =>
      write<ModelLogin>(
        scope.path(
          `/api/model-connections/${connection.id}/login/${operationId}/${action}`,
        ),
        "POST",
      ),
    onSuccess: (value) => {
      client.setQueryData(key, value);
    },
    onSettled: (value, error) =>
      error || value?.status !== "pending" ? refresh() : undefined,
  });
  const {
    mutate: advanceLogin,
    isPending: advancing,
    isError: advanceFailed,
  } = advance;
  const interval = current?.poll_interval ?? 5;
  const state = current?.status;
  useEffect(() => {
    if (state !== "pending" || advancing || advanceFailed || !operationId)
      return;
    const timer = window.setTimeout(
      () => advanceLogin("poll"),
      interval * 1000,
    );
    return () => window.clearTimeout(timer);
  }, [
    state,
    advancing,
    advanceFailed,
    operationId,
    interval,
    session.dataUpdatedAt,
    advanceLogin,
  ]);
  return (
    <VStack gap={4} as="section">
      <Heading level={2}>OpenAI Codex login</Heading>
      <Text as="p">
        Pause the connection and finish or cancel its queued reviews before
        changing the account.
      </Text>
      {operationId ? <Freshness query={session} quiet /> : null}
      {current?.status === "pending" ? (
        <VStack gap={3} role="status">
          {challenge?.user_code ? (
            <>
              <Text as="p">
                Open the verification page and enter this code:
              </Text>
              <Copy value={challenge.user_code} label="device code">
                <Code>{challenge.user_code}</Code>
              </Copy>
              <Text as="p">
                <AstryxLink
                  href={challenge.verification_url ?? undefined}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open auth.openai.com
                  <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
                </AstryxLink>
              </Text>
            </>
          ) : (
            <Text as="p">
              Continue in the provider verification tab, or cancel this
              login to request a new code.
            </Text>
          )}
          <Text as="p" color="secondary" display="block" type="supporting">
            Expires {time(current.expires_at)}. Checks every {interval}{" "}
            seconds while this page is open.
          </Text>
          <Button
            label={"Cancel login"}
            variant="secondary"
            type="button"
            isDisabled={advancing}
            onClick={() => advance.mutate("cancel")}
          />
        </VStack>
      ) : current ? (
        <Text as="p" role="status">
          {current.status === "approved"
            ? "Account recorded. Enable the connection when its model policy is ready."
            : current.status === "needs_attention"
              ? "The login result is uncertain. A platform owner must reconcile this connection."
              : `Login ${current.status.replaceAll("_", " ")}.`}
        </Text>
      ) : null}
      {start.isError || advance.isError ? (
        <Text as="p" role="alert">
          {(start.error ?? advance.error)?.message}
        </Text>
      ) : null}
      {connection.state === "disabled" && configured ? (
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            start.mutate();
          }}
        >
          <HStack gap={3} wrap="wrap" align="center">
            <Button
              label={String(
                start.isPending ? "Starting login…" : "Connect OpenAI Codex",
              )}
              variant="primary"
              type="submit"
              isDisabled={
                start.isPending ||
                !!connection.queued_jobs ||
                !!connection.leased_jobs ||
                !!connection.active_executions
              }
            />
          </HStack>
        </Form>
      ) : null}
    </VStack>
  );
}

function ConnectionContent({ connection }: { connection: Connection }) {
  const scope = useScope();
  const refresh = useConnectionRefresh();
  const runtime = useQuery({
    queryKey: ["model-runtime", connection.id, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<Runtime>(
        scope.path(`/api/model-connections/${connection.id}/runtime`),
        signal,
      ),
    enabled: connection.can_manage,
    retry: false,
    staleTime: 60_000,
  });
  const body = { expected_revision: connection.revision };
  const base = `/api/model-connections/${connection.id}`;
  const done = () => {
    void refresh();
  };
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>{connection.name}</Heading>
          <Text as="p">
            {connection.team_name
              ? `Dedicated to ${connection.team_name}`
              : "Platform-owned shared connection"}
          </Text>
        </VStack>
        <Link to="/model-connections">All connections</Link>
      </HStack>
      <VStack gap={4}>
        <MetadataList
          orientation="horizontal"
          columns="multi"
          label={{ position: "top" }}
        >
          <MetadataListItem label="Status">
            {stateNames[connection.state]}
          </MetadataListItem>
          <MetadataListItem label="Concurrent reviews">
            {connection.max_concurrency}
          </MetadataListItem>
          {connection.queued_jobs !== null && (
            <MetadataListItem label="Review workload">
              {connection.queued_jobs} queued · {connection.leased_jobs}{" "}
              claimed · {connection.active_executions} unfinished executions
            </MetadataListItem>
          )}
        </MetadataList>
        <VStack gap={2} maxWidth={760}>
          {connection.queued_jobs === null && (
            <Text as="p">
              Shared accounts are managed by a platform owner.
            </Text>
          )}
          {!!connection.active_executions && (
            <Text as="p">
              Unfinished executions reserve capacity until Hermes records
              completion. If this count remains after reviews stop, ask a
              platform owner to pause the connection and use “Recover after
              a runtime restart” below.
            </Text>
          )}
          {connection.state === "disabled" ? (
            <Text as="p">
              New dispatch is paused. Reviews already running can finish.
            </Text>
          ) : null}
          {connection.queued_jobs ||
          connection.leased_jobs ||
          connection.active_executions ? (
            <Text as="p">
              Account changes require these reviews to finish or be
              cancelled. <Link to="/history">View reviews</Link>.
            </Text>
          ) : null}
        </VStack>
        {connection.can_manage ? (
          <>
            <Freshness query={runtime} quiet />
            {runtime.data?.configured === false ? (
              <VStack gap={3}>
                <Text as="p">
                  Provider control is not configured for this runtime.
                  Existing review execution can continue.
                </Text>
                <AstryxLink
                  href="https://ccimen.github.io/review-agent/admin-panel#managed-model-connections"
                  target="_blank"
                  rel="noreferrer"
                >
                  Open setup instructions
                </AstryxLink>
              </VStack>
            ) : null}
            <HStack gap={3} wrap="wrap" vAlign="center">
              {connection.state === "enabled" ||
              connection.state === "disabled" ? (
                <ConfirmAction
                  key={`enabled:${connection.revision}`}
                  label={
                    connection.state === "enabled"
                      ? "Pause connection"
                      : "Enable connection"
                  }
                  path={scope.path(`${base}/enabled`)}
                  body={{
                    ...body,
                    enabled: connection.state !== "enabled",
                  }}
                  description={
                    connection.state === "enabled"
                      ? "Stop new dispatch. Running reviews can finish; queued reviews retain their admitted account."
                      : "Verify the recorded account and allow queued reviews to run."
                  }
                  done={done}
                />
              ) : null}
              {connection.state === "disabled" &&
              runtime.data?.configured ? (
                <ConfirmAction
                  key={`reconcile:${connection.revision}`}
                  label="Record current accounts"
                  path={scope.path(`${base}/reconcile`)}
                  body={body}
                  description="Record an account connected or removed by the operator in this runtime. Queued and running reviews must be cleared first. The connection stays paused."
                  done={done}
                />
              ) : null}
            </HStack>
          </>
        ) : null}
      </VStack>
      <VStack gap={4} as="section">
        <Heading level={2}>Provider accounts</Heading>

        <Table aria-label="Provider account observations">
          <TableHeader>
            <TableRow>
              <TableHeaderCell>Provider</TableHeaderCell>
              <TableHeaderCell>Account record</TableHeaderCell>
              <TableHeaderCell>Observed</TableHeaderCell>
              {connection.can_manage ? (
                <TableHeaderCell>Runtime observation</TableHeaderCell>
              ) : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {connection.accounts.map((account) => {
              const observed = runtime.data?.accounts.find(
                (item) => item.provider === account.provider,
              );
              const status = observed?.availability;
              const descriptions = {
                available: "One credential found",
                disconnected: "No credential found",
                multiple_accounts:
                  "Multiple credentials · Operator action required",
                identity_unavailable: "Account identity unavailable",
                isolation_required: "Private credential storage required",
              };
              return (
                <TableRow key={account.provider}>
                  <TableHeaderCell scope="row">
                    {providerNames[account.provider]}
                    {account.label ? (
                      <Text
                        color="secondary"
                        display="block"
                        type="supporting"
                      >
                        {account.label}
                      </Text>
                    ) : null}
                  </TableHeaderCell>
                  <TableCell>
                    {account.verified
                      ? `Recorded · Revision ${account.revision}`
                      : "No identity recorded"}
                  </TableCell>
                  <TableCell>{time(account.observed_at)}</TableCell>
                  {connection.can_manage ? (
                    <TableCell>
                      {status ? descriptions[status] : "Unavailable"}
                    </TableCell>
                  ) : null}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>

        <Text as="p" color="secondary">
          These observations describe local credentials. They do not confirm
          provider availability or remaining quota.
        </Text>
      </VStack>
      <ConnectionQuota connection={connection} />
      {connection.can_manage && connection.state !== "retired" ? (
        <ConnectionLogin
          connection={connection}
          configured={runtime.data?.configured === true}
        />
      ) : null}
      {connection.can_manage ? (
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Anthropic and operator-managed credentials
            </HStack>
          }
        >
          <VStack gap={4}>
            <Text as="p">
              Pause this connection, clear queued and running reviews, then
              have the platform operator connect or remove the authorized
              provider credential in this runtime's private Hermes home.
              Choose Record current accounts before enabling it.
            </Text>
            <Text as="p">
              Anthropic API keys are supported. Provider secrets are never
              entered in this console.
            </Text>
          </VStack>
        </Collapsible>
      ) : null}
      {scope.current.role === "owner" &&
      (connection.state === "needs_attention" ||
        connection.state === "authenticating" ||
        !!connection.active_executions) ? (
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Recover after a runtime restart
            </HStack>
          }
        >
          <VStack gap={4}>
            <Text as="p">
              Stop and restart this connection's complete runtime, including
              its provider control and login service. Confirm the old
              processes have stopped, then cancel remaining queued or
              claimed reviews. Recovery closes interrupted operations and
              records the current accounts.
            </Text>
            <ConfirmAction
              key={`recover:${connection.revision}`}
              label="Confirm restart and reconcile"
              path={scope.path(`${base}/reconcile`)}
              body={{ ...body, runtime_restarted: true }}
              description="I confirm all old runtime, login, and provider-control processes for this connection have stopped. The connection stays paused after recovery."
              done={done}
            />
          </VStack>
        </Collapsible>
      ) : null}
      {connection.can_configure &&
      ["enabled", "disabled"].includes(connection.state) ? (
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Edit name, capacity, and allowed models
            </HStack>
          }
        >
          <VStack gap={4}>
            <ConnectionEditor
              key={connection.revision}
              connection={connection}
            />
          </VStack>
        </Collapsible>
      ) : null}
      {connection.can_configure &&
      connection.runtime_key !== "shared" &&
      connection.state === "disabled" ? (
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Retire this connection
            </HStack>
          }
        >
          <VStack gap={4}>
            <ConfirmAction
              key={`retire:${connection.revision}`}
              label="Retire connection"
              path={scope.path(`${base}/retire`)}
              body={body}
              description="Assign its teams elsewhere and clear queued or running reviews first. Review and audit history remain available."
              danger
              done={done}
            />
          </VStack>
        </Collapsible>
      ) : null}
    </>
  );
}

export function ModelConnectionPage() {
  const { connectionId } = useParams();
  const scope = useScope();
  const query = useQuery({
    queryKey: [
      "model-connections",
      "detail",
      connectionId,
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<Connection>(
        scope.path(`/api/model-connections/${connectionId}`),
        signal,
      ),
    retry: false,
  });
  useEffect(() => {
    document.title = `Review Agent · ${query.data?.name ?? "Model connection"}`;
  }, [query.data?.name]);
  return (
    <>
      <Freshness query={query} />
      {query.data ? (
        <ConnectionContent key={query.data.id} connection={query.data} />
      ) : null}
    </>
  );
}
