import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { APIError, read, write } from "./api";
import type { Account } from "./api";
import { Copy, Empty, Freshness, time } from "./ui";

type ProviderPage = components["schemas"]["ProviderPage"];
type ProviderModelPage = components["schemas"]["ProviderModelPage"];
type LoginSession = components["schemas"]["LoginSession"];
type Cancellation = components["schemas"]["Cancellation"];

const terminalLoginStatuses = new Set([
  "approved",
  "denied",
  "expired",
  "error",
]);

const message = (error: unknown) =>
  error instanceof APIError
    ? error.message
    : "Hermes provider control is unavailable.";

export function Providers({ current }: { current?: Account }) {
  const isAdmin = current?.role !== "viewer";
  const client = useQueryClient();
  const [session, setSession] = useState<LoginSession | null>(null);
  const [deadline, setDeadline] = useState<number | null>(null);
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: ({ signal }) => read<ProviderPage>("/api/providers", signal),
    enabled: isAdmin,
  });
  const models = useQuery({
    queryKey: ["providers", "models"],
    queryFn: ({ signal }) =>
      read<ProviderModelPage>("/api/providers/models", signal),
    enabled: isAdmin && providers.data?.capability.configured === true,
  });
  const start = useMutation({
    mutationFn: () =>
      write<LoginSession>("/api/providers/openai-codex/login", "POST"),
    onSuccess: (value) => {
      setSession(value);
      setDeadline(Date.now() + (value.expires_in ?? 0) * 1000);
    },
  });
  const poll = useQuery({
    queryKey: ["providers", "login", session?.session_id],
    queryFn: ({ signal }) =>
      read<LoginSession>(
        `/api/providers/openai-codex/login/${encodeURIComponent(session!.session_id)}`,
        signal,
      ),
    enabled: session?.status === "pending",
    refetchInterval: (query) =>
      query.state.data?.status === "pending" || query.state.data === undefined
        ? 5000
        : false,
    refetchOnWindowFocus: false,
  });
  const cancel = useMutation({
    mutationFn: () =>
      write<Cancellation>(
        `/api/providers/openai-codex/login/${encodeURIComponent(session!.session_id)}/cancel`,
        "POST",
      ),
    onSuccess: () => {
      setSession(null);
      setDeadline(null);
    },
  });
  const status =
    session && terminalLoginStatuses.has(session.status)
      ? session.status
      : (poll.data?.status ?? session?.status);
  useEffect(() => {
    if (!poll.data || !terminalLoginStatuses.has(poll.data.status)) return;
    setSession((value) =>
      value?.session_id === poll.data.session_id
        ? { ...value, status: poll.data.status }
        : value,
    );
  }, [poll.data]);
  useEffect(() => {
    if (status !== "approved") return;
    void client.invalidateQueries({ queryKey: ["providers"] });
    setSession(null);
    setDeadline(null);
  }, [client, status]);
  useEffect(() => {
    if (deadline === null || session === null || session.status !== "pending")
      return;
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      setSession((value) => (value ? { ...value, status: "expired" } : value));
      return;
    }
    const timer = window.setTimeout(
      () =>
        setSession((value) =>
          value ? { ...value, status: "expired" } : value,
        ),
      remaining,
    );
    return () => window.clearTimeout(timer);
  }, [deadline, session]);

  const codex = providers.data?.items.find(
    (item) => item.provider === "openai-codex",
  );
  const anthropic = providers.data?.items.find(
    (item) => item.provider === "anthropic",
  );
  function reconnect() {
    start.reset();
    cancel.reset();
    if (session) {
      client.removeQueries({
        queryKey: ["providers", "login", session.session_id],
      });
    }
    setSession(null);
    setDeadline(null);
    start.mutate();
  }

  if (!isAdmin) {
    return (
      <Empty title="Administrator access required">
        Provider connections are managed by administrators.
      </Empty>
    );
  }

  return (
    <section>
      <div className="section-heading">
        <div>
          <h2>Model providers</h2>
          <p>Provider credentials are held and refreshed by Hermes.</p>
        </div>
      </div>
      <Freshness query={providers} quiet />
      {providers.data && !providers.data.capability.configured ? (
        <Empty title="Provider control is not configured">
          Configure the Hermes dashboard control connection for this deployment.
        </Empty>
      ) : null}
      {codex ? (
        <article className="panel provider-card">
          <div className="panel-heading">
            <div>
              <strong>{codex.name}</strong>
              <span className="subtext">OpenAI Codex device login</span>
            </div>
            <span className={`status ${codex.connected ? "published" : ""}`}>
              {codex.connected ? "Connected" : "Not connected"}
            </span>
          </div>
          <div className="panel-body">
            {status === "pending" && session ? (
              <div className="provider-login" role="status">
                <p>Open the verification page and enter this one-time code:</p>
                <p>
                  <Copy value={session.user_code ?? ""} label="device code">
                    <code>{session.user_code}</code>
                  </Copy>
                </p>
                <p>
                  <a
                    className="button secondary"
                    href={session.verification_url ?? undefined}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open auth.openai.com
                  </a>
                </p>
                <p className="subtext">
                  Waiting for approval. This page checks every five seconds.
                </p>
                {poll.isError ? (
                  <p className="form-error">{message(poll.error)}</p>
                ) : null}
                {cancel.isError ? (
                  <p className="form-error">{message(cancel.error)}</p>
                ) : null}
                <button
                  className="secondary"
                  type="button"
                  disabled={cancel.isPending}
                  onClick={() => cancel.mutate()}
                >
                  Cancel login
                </button>
              </div>
            ) : status === "expired" ||
              status === "error" ||
              status === "denied" ? (
              <div className="notice error">
                <p>
                  {status === "expired"
                    ? "The login code expired."
                    : status === "denied"
                      ? "The login request was denied."
                      : "Hermes could not complete this login."}
                </p>
                <button type="button" onClick={reconnect}>
                  Reconnect
                </button>
              </div>
            ) : !codex.connected ? (
              <button
                type="button"
                disabled={start.isPending}
                onClick={reconnect}
              >
                Connect
              </button>
            ) : (
              <p className="subtext">
                Hermes reports an active subscription login
                {codex.expires_at ? ` through ${time(codex.expires_at)}` : ""}.
              </p>
            )}
            {start.isError ? (
              <p className="form-error">{message(start.error)}</p>
            ) : null}
          </div>
        </article>
      ) : null}
      {anthropic ? (
        <article className="panel provider-card">
          <div className="panel-heading">
            <div>
              <strong>{anthropic.name}</strong>
              <span className="subtext">External CLI authentication</span>
            </div>
            <span
              className={`status ${anthropic.connected ? "published" : ""}`}
            >
              {anthropic.connected ? "Connected" : "Not connected"}
            </span>
          </div>
          <div className="panel-body">
            <p>Authenticate from the Hermes host:</p>
            <Copy
              value="hermes auth add anthropic"
              label="Anthropic login command"
            >
              <code>hermes auth add anthropic</code>
            </Copy>
          </div>
        </article>
      ) : null}
      {models.data?.items.length ? (
        <details className="panel">
          <summary>Available models</summary>
          <div className="panel-body">
            {(["openai-codex", "anthropic"] as const).map((provider) => (
              <div key={provider}>
                <h3>
                  {provider === "openai-codex" ? "OpenAI Codex" : "Anthropic"}
                </h3>
                <ul>
                  {models
                    .data!.items.filter((item) => item.provider === provider)
                    .map((item) => (
                      <li key={`${provider}:${item.model}`}>
                        <code>{item.model}</code>
                      </li>
                    ))}
                </ul>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}
