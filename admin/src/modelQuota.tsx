import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { read } from "./api";
import { useScope } from "./scope";
import { number, time } from "./ui";

type Connection = components["schemas"]["ModelConnection"];
type AccountQuota = components["schemas"]["AccountQuota"];

function windowDuration(seconds: number | null) {
  if (seconds === null) return "Duration unknown";
  for (const [unit, size] of [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ] as const) {
    if (seconds % size === 0) {
      const value = seconds / size;
      return `${number.format(value)} ${unit}${value === 1 ? "" : "s"}`;
    }
  }
  return `${number.format(seconds)} seconds`;
}

const quotaTime = (seconds: number | null) =>
  seconds === null ? "Unknown" : time(new Date(seconds * 1000).toISOString());

export function ConnectionQuota({ connection }: { connection: Connection }) {
  const scope = useScope();
  const client = useQueryClient();
  const account = connection.accounts.find(
    (item) => item.provider === "openai-codex",
  );
  const enabled = !!account?.verified && connection.state !== "retired";
  const key = [
    "model-quota",
    connection.id,
    connection.revision,
    account?.revision,
    "scoped",
    scope.key,
  ];
  const path = `/api/model-connections/${connection.id}/quota/openai-codex`;
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => read<AccountQuota>(scope.path(path), signal),
    enabled,
    retry: false,
    staleTime: 30_000,
    refetchInterval: (state) => {
      if (state.state.error) return false;
      const data = state.state.data;
      if (!data) return false;
      if (data.refreshing) return 2_000;
      return data.next_refresh_at === null
        ? false
        : Math.max(2_000, data.next_refresh_at * 1000 - Date.now());
    },
  });
  const refresh = useMutation({
    mutationFn: async () => {
      await client.cancelQueries({ queryKey: key, exact: true });
      return client.fetchQuery({
        queryKey: key,
        queryFn: ({ signal }) =>
          read<AccountQuota>(scope.path(`${path}?refresh=true`), signal),
        staleTime: 0,
      });
    },
  });
  const error = query.error ?? refresh.error;
  const data = error ? undefined : query.data;
  const snapshot = data?.snapshot;
  return (
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>Account quota</h2>
          <p>
            {connection.team_id === null
              ? "This shared account includes usage by other teams and outside Review Agent."
              : "Provider totals for this account include any use outside Review Agent."}{" "}
            Team review activity is available in the Overview.
          </p>
        </div>
        {enabled ? (
          <button
            className="secondary"
            disabled={refresh.isPending || data?.refreshing}
            onClick={() => refresh.mutate()}
          >
            {refresh.isPending || data?.refreshing
              ? "Refreshing quota…"
              : "Refresh quota"}
          </button>
        ) : null}
      </div>
      {!enabled ? (
        <p>
          OpenAI Codex quota is unavailable until a connection maintainer
          verifies the account.
        </p>
      ) : error ? (
        <div className="notice error" role="alert">
          <p>{error.message}</p>
          <button
            onClick={() => {
              refresh.reset();
              void query.refetch();
            }}
          >
            Retry quota
          </button>
        </div>
      ) : query.isPending ? (
        <p role="status">Loading account quota…</p>
      ) : null}
      {data?.refreshing ? (
        <p role="status">Checking the provider for current quota.</p>
      ) : null}
      {data?.unavailable_reason === "provider_unavailable" ? (
        <p className="notice">
          The provider could not be reached.{" "}
          {snapshot
            ? "The last successful observation is shown below."
            : "Quota is currently unknown."}
          {data.next_refresh_at !== null
            ? ` Next retry: ${quotaTime(data.next_refresh_at)}.`
            : ""}
        </p>
      ) : null}
      {data?.unavailable_reason === "account_unavailable" ? (
        <p className="notice">
          The provider account is unavailable. A connection maintainer can check
          its credentials.
        </p>
      ) : null}
      {snapshot ? (
        <>
          <p className="field-help">
            OpenAI Codex{snapshot.plan ? ` · ${snapshot.plan}` : ""} · Last
            successful check {quotaTime(snapshot.fetched_at)}
            {data?.stale
              ? " · Stale observation; remaining quota may have changed"
              : ""}
          </p>
          {snapshot.spend_control_reached ? (
            <p className="notice">
              The provider reports that an account spending limit has been
              reached.
            </p>
          ) : null}
          {snapshot.limit_reached_type ? (
            <p>
              Provider limit: {snapshot.limit_reached_type.replaceAll("_", " ")}
            </p>
          ) : null}
          {snapshot.buckets.length ? (
            <div
              className="panel table-scroll"
              role="region"
              tabIndex={0}
              aria-label="Provider quota windows"
            >
              <table>
                <thead>
                  <tr>
                    <th>Quota bucket</th>
                    <th>Window</th>
                    <th>Remaining</th>
                    <th>Resets</th>
                    <th>Provider status</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.buckets.flatMap((bucket) => {
                    const windows = bucket.windows.length
                      ? bucket.windows
                      : [null];
                    return windows.map((window) => (
                      <tr key={`${bucket.id}:${window?.kind ?? "unknown"}`}>
                        <th scope="row">
                          {bucket.name ??
                            (bucket.id === "codex" ? "Codex" : bucket.id)}
                          {bucket.normal_model_slug ? (
                            <span className="subtext">
                              Provider model label: {bucket.normal_model_slug}
                            </span>
                          ) : null}
                        </th>
                        <td>
                          {window
                            ? windowDuration(window.duration_seconds)
                            : "Unknown"}
                          {window ? (
                            <span className="subtext">
                              {window.kind === "primary"
                                ? "Primary"
                                : "Secondary"}
                            </span>
                          ) : null}
                        </td>
                        <td>
                          {window?.used_percent !== null &&
                          window?.used_percent !== undefined ? (
                            <>
                              {number.format(
                                Math.max(0, 100 - window.used_percent),
                              )}
                              %
                              <span className="subtext">
                                {number.format(window.used_percent)}% used
                              </span>
                            </>
                          ) : (
                            "Unknown"
                          )}
                        </td>
                        <td>{quotaTime(window?.resets_at ?? null)}</td>
                        <td>
                          {bucket.allowed === true
                            ? "Usage allowed"
                            : bucket.allowed === false
                              ? "Usage paused"
                              : bucket.limit_reached
                                ? "Limit reached"
                                : "Unknown"}
                        </td>
                      </tr>
                    ));
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p>No quota windows were reported by the provider.</p>
          )}
          <p>
            Available usage resets:{" "}
            {snapshot.reset_credits_available === null
              ? "Unknown"
              : number.format(snapshot.reset_credits_available)}
            .
          </p>
          <p className="field-help">
            Window lengths, bucket names, and model labels come from the
            provider. A passed reset time needs a new observation to confirm
            recovery. Manual refreshes are limited to once every 30 seconds.
          </p>
        </>
      ) : null}
      <p className="field-help">
        Anthropic API keys do not expose subscription quota through this
        connection. Quota is unknown.
      </p>
    </section>
  );
}
