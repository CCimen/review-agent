import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { read, write } from "./api";
import type { TeamPage } from "./api";
import { ScopedLink as Link, useScope } from "./scope";
import { Copy, Empty, Freshness, time } from "./ui";
import { ReasonAction } from "./teams";

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
  const [runtimeKey, setRuntimeKey] = useState("");
  const [teamId, setTeamId] = useState(scope.teamId ?? "");
  const [teamSearch, setTeamSearch] = useState("");
  const [choices, setChoices] = useState<ModelChoice[]>(
    connection?.allowed_routes ?? [],
  );
  const [reason, setReason] = useState("");
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
              expected_revision: connection.revision,
              reason,
            } satisfies components["schemas"]["ConnectionUpdate"],
          )
        : write<Connection>("/api/model-connections", "POST", {
            name,
            runtime_key: runtimeKey,
            team_id: teamId ? Number(teamId) : null,
            allowed_routes: choices,
            reason,
          } satisfies components["schemas"]["ConnectionCreate"]),
    onSuccess: async (value) => {
      await refresh();
      setReason("");
      done?.();
      if (!connection)
        navigate(
          `/model-connections/${value.id}${value.team_id ? `?team_id=${value.team_id}` : ""}`,
        );
    },
  });
  function updateChoice(index: number, value: Partial<ModelChoice>) {
    setChoices((items) =>
      items.map((item, at) => (at === index ? { ...item, ...value } : item)),
    );
  }
  return (
    <form
      className="team-editor"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <label className="field">
        Connection name
        <input
          required
          maxLength={80}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </label>
      {!connection ? (
        <>
          <Freshness query={runtimes} />
          <label className="field">
            Provisioned runtime
            <select
              required
              value={runtimeKey}
              onChange={(event) => setRuntimeKey(event.target.value)}
            >
              <option value="">Choose a runtime</option>
              {runtimes.data
                ?.filter((key) => key !== "shared")
                .map((key) => (
                  <option key={key} value={key}>
                    {key}
                  </option>
                ))}
            </select>
          </label>
          <p className="field-help">
            A platform operator provisions each runtime and its private
            credential storage first.
          </p>
          {runtimes.data && !runtimes.data.some((key) => key !== "shared") ? (
            <p>
              No additional runtime is configured.{" "}
              <a
                href="https://ccimen.github.io/review-agent/admin-panel#managed-model-connections"
                target="_blank"
                rel="noreferrer"
              >
                Open setup instructions
              </a>
              .
            </p>
          ) : null}
          <label className="field">
            Find an owning team
            <input
              type="search"
              maxLength={80}
              placeholder="Type at least two characters"
              value={teamSearch}
              onChange={(event) => setTeamSearch(event.target.value)}
            />
          </label>
          {teamSearch.trim().length >= 2 ? <Freshness query={teams} /> : null}
          <label className="field">
            Connection owner
            <select
              value={teamId}
              onChange={(event) => setTeamId(event.target.value)}
            >
              <option value="">Platform · Shared connection</option>
              {teamId &&
              !teams.data?.items.some((team) => String(team.id) === teamId) ? (
                <option value={teamId}>
                  {String(scope.team?.id) === teamId
                    ? scope.team?.name
                    : `Team ${teamId}`}
                </option>
              ) : null}
              {teams.data?.items.map((team) => (
                <option value={team.id} key={team.id}>
                  {team.name}
                </option>
              ))}
            </select>
          </label>
          {teams.data?.next_after_id ? (
            <p className="field-help">
              Refine the team name to find more results.
            </p>
          ) : null}
        </>
      ) : null}
      <h3>Allowed team choices</h3>
      <p>
        Teams may inherit deployment defaults or choose one of these models and
        reasoning levels.
      </p>
      <div className="model-choices">
        {choices.map((choice, index) => (
          <fieldset className="model-choice" key={index}>
            <legend>Model {index + 1}</legend>
            <div className="form-fields">
              <label className="field">
                Provider
                <select
                  value={choice.provider}
                  onChange={(event) =>
                    updateChoice(index, {
                      provider: event.target.value as ModelChoice["provider"],
                    })
                  }
                >
                  <option value="openai-codex">OpenAI Codex</option>
                  <option value="anthropic">Anthropic</option>
                </select>
              </label>
              <label className="field grow">
                Model ID
                <input
                  required
                  maxLength={200}
                  value={choice.model}
                  onChange={(event) =>
                    updateChoice(index, { model: event.target.value })
                  }
                />
              </label>
            </div>
            <div className="model-efforts">
              {effortChoices.map((effort) => (
                <label key={effort}>
                  <input
                    type="checkbox"
                    checked={choice.reasoning_efforts.includes(effort)}
                    onChange={(event) =>
                      updateChoice(index, {
                        reasoning_efforts: event.target.checked
                          ? [...choice.reasoning_efforts, effort]
                          : choice.reasoning_efforts.filter(
                              (value) => value !== effort,
                            ),
                      })
                    }
                  />
                  {effort}
                </label>
              ))}
            </div>
            <button
              type="button"
              className="text-button danger"
              onClick={() =>
                setChoices((items) => items.filter((_item, at) => at !== index))
              }
            >
              Remove model
            </button>
          </fieldset>
        ))}
      </div>
      <button
        type="button"
        className="secondary"
        disabled={choices.length >= 50}
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
      >
        Add a model choice
      </button>
      <label className="field">
        Reason
        <textarea
          required
          maxLength={500}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      {save.isError ? (
        <p className="notice error" role="alert">
          {save.error.message}
        </p>
      ) : null}
      {save.isSuccess ? <p role="status">Connection saved.</p> : null}
      <button
        disabled={
          save.isPending ||
          choices.some((choice) => choice.reasoning_efforts.length === 0)
        }
      >
        {save.isPending
          ? "Saving…"
          : connection
            ? "Save connection"
            : "Add connection"}
      </button>
    </form>
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
      <div className="page-heading">
        <div>
          <h1>Model connections</h1>
          <p>
            {scope.team
              ? `Provider accounts available to ${scope.team.name}.`
              : "Shared and team-owned provider accounts."}
          </p>
        </div>
        {scope.current.role === "owner" ? (
          <button onClick={() => setAdding(!adding)} aria-expanded={adding}>
            {adding ? "Close form" : "Add connection"}
          </button>
        ) : null}
      </div>
      {adding ? (
        <section className="panel panel-body">
          <h2>Add a managed connection</h2>
          <ConnectionEditor done={() => setAdding(false)} />
        </section>
      ) : null}
      <Freshness query={query} />
      {query.data?.items.length ? (
        <div
          className="panel table-scroll"
          role="region"
          tabIndex={0}
          aria-label="Model connections"
        >
          <table>
            <thead>
              <tr>
                <th>Connection</th>
                <th>Owner</th>
                <th>Status</th>
                <th>Recorded accounts</th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((connection) => (
                <tr key={connection.id}>
                  <th scope="row">
                    <Link to={`/model-connections/${connection.id}`}>
                      {connection.name}
                    </Link>
                  </th>
                  <td>{connection.team_name ?? "Platform · Shared"}</td>
                  <td>
                    <span
                      className={`status ${connection.state === "enabled" ? "published" : connection.state === "needs_attention" ? "failed" : "queued"}`}
                    >
                      {stateNames[connection.state]}
                    </span>
                  </td>
                  <td>
                    {connection.accounts.some((account) => account.verified)
                      ? connection.accounts
                          .filter((account) => account.verified)
                          .map((account) => providerNames[account.provider])
                          .join(", ")
                      : "No identity recorded"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : query.data ? (
        <Empty title="No model connections in this view">
          An administrator can assign a shared or dedicated connection to your
          team.
        </Empty>
      ) : null}
      {after !== "0" || query.data?.next_after_id ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={after === "0"}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("after_id");
              setParams(next);
            }}
          >
            First page
          </button>
          <button
            className="secondary"
            disabled={!query.data?.next_after_id}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("after_id", String(query.data?.next_after_id));
              setParams(next);
            }}
          >
            Next connections
          </button>
        </div>
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
  const [reason, setReason] = useState("");
  const operationId = challenge?.id ?? connection.active_login_id;
  const key = ["model-login", connection.id, operationId, "scoped", scope.key];
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
        reason,
      } satisfies components["schemas"]["ConnectionChange"]),
    onSuccess: (value) => {
      setChallenge(value);
      setReason("");
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
    <section className="section">
      <h2>OpenAI Codex login</h2>
      <p>
        Pause the connection and finish or cancel its queued reviews before
        changing the account.
      </p>
      {operationId ? <Freshness query={session} quiet /> : null}
      {current?.status === "pending" ? (
        <div className="provider-login" role="status">
          {challenge?.user_code ? (
            <>
              <p>Open the verification page and enter this code:</p>
              <Copy value={challenge.user_code} label="device code">
                <code>{challenge.user_code}</code>
              </Copy>
              <p>
                <a
                  className="button secondary"
                  href={challenge.verification_url ?? undefined}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open auth.openai.com
                  <span className="sr-only"> (opens in a new tab)</span>
                </a>
              </p>
            </>
          ) : (
            <p>
              Continue in the provider verification tab, or cancel this login to
              request a new code.
            </p>
          )}
          <p className="subtext">
            Expires {time(current.expires_at)}. Checks every {interval} seconds
            while this page is open.
          </p>
          <button
            type="button"
            className="secondary"
            disabled={advancing}
            onClick={() => advance.mutate("cancel")}
          >
            Cancel login
          </button>
        </div>
      ) : current ? (
        <p role="status">
          {current.status === "approved"
            ? "Account recorded. Enable the connection when its model policy is ready."
            : current.status === "needs_attention"
              ? "The login result is uncertain. A platform owner must reconcile this connection."
              : `Login ${current.status.replaceAll("_", " ")}.`}
        </p>
      ) : null}
      {start.isError || advance.isError ? (
        <p className="notice error" role="alert">
          {(start.error ?? advance.error)?.message}
        </p>
      ) : null}
      {connection.state === "disabled" && configured ? (
        <form
          className="reason-form"
          onSubmit={(event) => {
            event.preventDefault();
            start.mutate();
          }}
        >
          <label className="field">
            Reason for connecting
            <textarea
              required
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <button
            disabled={
              start.isPending ||
              !!connection.queued_jobs ||
              !!connection.leased_jobs ||
              !!connection.active_executions
            }
          >
            {start.isPending ? "Starting login…" : "Connect OpenAI Codex"}
          </button>
        </form>
      ) : null}
    </section>
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
      <div className="page-heading">
        <div>
          <h1>{connection.name}</h1>
          <p>
            {connection.team_name
              ? `Dedicated to ${connection.team_name}`
              : "Platform-owned shared connection"}
          </p>
        </div>
        <Link className="button secondary" to="/model-connections">
          All connections
        </Link>
      </div>
      <p>
        <span
          className={`status ${connection.state === "enabled" ? "published" : connection.state === "needs_attention" ? "failed" : "queued"}`}
        >
          {stateNames[connection.state]}
        </span>
      </p>
      {connection.queued_jobs !== null ? (
        <p>
          {connection.queued_jobs} queued · {connection.leased_jobs} claimed ·{" "}
          {connection.active_executions} executing
        </p>
      ) : (
        <p>Shared accounts are managed by a platform owner.</p>
      )}
      {connection.state === "disabled" ? (
        <p>New dispatch is paused. Reviews already running can finish.</p>
      ) : null}
      {connection.queued_jobs ||
      connection.leased_jobs ||
      connection.active_executions ? (
        <p>
          Account changes require these reviews to finish or be cancelled.{" "}
          <Link to="/history">View reviews</Link>.
        </p>
      ) : null}
      {connection.can_manage ? (
        <>
          <Freshness query={runtime} quiet />
          {runtime.data?.configured === false ? (
            <div className="notice">
              <p>
                Provider control is not configured for this runtime. Existing
                review execution can continue.
              </p>
              <a
                href="https://ccimen.github.io/review-agent/admin-panel#managed-model-connections"
                target="_blank"
                rel="noreferrer"
              >
                Open setup instructions
              </a>
            </div>
          ) : null}
          <div className="inline-actions">
            {connection.state === "enabled" ||
            connection.state === "disabled" ? (
              <ReasonAction
                key={`enabled:${connection.revision}`}
                label={
                  connection.state === "enabled"
                    ? "Pause connection"
                    : "Enable connection"
                }
                path={scope.path(`${base}/enabled`)}
                body={{ ...body, enabled: connection.state !== "enabled" }}
                description={
                  connection.state === "enabled"
                    ? "Stop new dispatch. Running reviews can finish; queued reviews retain their admitted account."
                    : "Verify the recorded account and allow queued reviews to run."
                }
                done={done}
              />
            ) : null}
            {connection.state === "disabled" && runtime.data?.configured ? (
              <ReasonAction
                key={`reconcile:${connection.revision}`}
                label="Record current accounts"
                path={scope.path(`${base}/reconcile`)}
                body={body}
                description="Record an account connected or removed by the operator in this runtime. Queued and running reviews must be cleared first. The connection stays paused."
                done={done}
              />
            ) : null}
          </div>
        </>
      ) : null}
      <section className="section">
        <h2>Provider accounts</h2>
        <div
          className="panel table-scroll"
          role="region"
          tabIndex={0}
          aria-label="Provider account observations"
        >
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Account record</th>
                <th>Observed</th>
                {connection.can_manage ? <th>Runtime observation</th> : null}
              </tr>
            </thead>
            <tbody>
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
                  <tr key={account.provider}>
                    <th scope="row">
                      {providerNames[account.provider]}
                      {account.label ? (
                        <span className="subtext">{account.label}</span>
                      ) : null}
                    </th>
                    <td>
                      {account.verified
                        ? `Recorded · Revision ${account.revision}`
                        : "No identity recorded"}
                    </td>
                    <td>{time(account.observed_at)}</td>
                    {connection.can_manage ? (
                      <td>{status ? descriptions[status] : "Unavailable"}</td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="field-help">
          These observations describe local credentials. They do not confirm
          provider availability or remaining quota.
        </p>
      </section>
      {connection.can_manage && connection.state !== "retired" ? (
        <ConnectionLogin
          connection={connection}
          configured={runtime.data?.configured === true}
        />
      ) : null}
      {connection.can_manage ? (
        <details className="section-disclosure">
          <summary>Anthropic and operator-managed credentials</summary>
          <p>
            Pause this connection, clear queued and running reviews, then have
            the platform operator connect or remove the authorized provider
            credential in this runtime's private Hermes home. Choose Record
            current accounts before enabling it.
          </p>
          <p>
            Anthropic API keys are supported. Provider secrets are never entered
            in this console.
          </p>
        </details>
      ) : null}
      {scope.current.role === "owner" &&
      (connection.state === "needs_attention" ||
        connection.state === "authenticating" ||
        !!connection.active_executions) ? (
        <details className="section-disclosure">
          <summary>Recover after a runtime restart</summary>
          <p>
            Stop and restart this connection's complete runtime, including its
            provider control and login service. Confirm the old processes have
            stopped, then cancel remaining queued or claimed reviews. Recovery
            closes interrupted operations and records the current accounts.
          </p>
          <ReasonAction
            key={`recover:${connection.revision}`}
            label="Confirm restart and reconcile"
            path={scope.path(`${base}/reconcile`)}
            body={{ ...body, runtime_restarted: true }}
            description="I confirm all old runtime, login, and provider-control processes for this connection have stopped. The connection stays paused after recovery."
            done={done}
          />
        </details>
      ) : null}
      {connection.can_configure &&
      ["enabled", "disabled"].includes(connection.state) ? (
        <details className="section-disclosure">
          <summary>Edit name and allowed models</summary>
          <ConnectionEditor key={connection.revision} connection={connection} />
        </details>
      ) : null}
      {connection.can_configure &&
      connection.runtime_key !== "shared" &&
      connection.state === "disabled" ? (
        <details className="section-disclosure">
          <summary>Retire this connection</summary>
          <ReasonAction
            key={`retire:${connection.revision}`}
            label="Retire connection"
            path={scope.path(`${base}/retire`)}
            body={body}
            description="Assign its teams elsewhere and clear queued or running reviews first. Review and audit history remain available."
            danger
            done={done}
          />
        </details>
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
