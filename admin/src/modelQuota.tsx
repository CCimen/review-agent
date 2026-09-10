import { Button } from "@astryxdesign/core/Button";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { read } from "./api";
import type { components } from "./api.generated";
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
    <VStack gap={4} as="section">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={2}>Account quota</Heading>
          <Text as="p">
            {connection.team_id === null
              ? "This shared account includes usage by other teams and outside Review Agent."
              : "Provider totals for this account include any use outside Review Agent."}{" "}
            Team review activity is available in the Overview.
          </Text>
        </VStack>
        {enabled ? (
          <Button
            label={String(
              refresh.isPending || data?.refreshing
                ? "Refreshing quota…"
                : "Refresh quota",
            )}
            variant="secondary"
            type="submit"

            isDisabled={refresh.isPending || data?.refreshing}
            onClick={() => refresh.mutate()}
          />
        ) : null}
      </HStack>
      {!enabled ? (
        <Text as="p">
          OpenAI Codex quota is unavailable until a connection maintainer
          verifies the account.
        </Text>
      ) : error ? (
        <VStack gap={3} role="alert">
          <Text as="p">{error.message}</Text>
          <Button
            label={"Retry quota"}
            variant="primary"
            type="submit"
            onClick={() => {
              refresh.reset();
              void query.refetch();
            }}
          />
        </VStack>
      ) : query.isPending ? (
        <Text as="p" role="status">
          Loading account quota…
        </Text>
      ) : null}
      {data?.refreshing ? (
        <Text as="p" role="status">
          Checking the provider for current quota.
        </Text>
      ) : null}
      {data?.unavailable_reason === "provider_unavailable" ? (
        <Text as="p">
          The provider could not be reached.{" "}
          {snapshot
            ? "The last successful observation is shown below."
            : "Quota is currently unknown."}
          {data.next_refresh_at !== null
            ? ` Next retry: ${quotaTime(data.next_refresh_at)}.`
            : ""}
        </Text>
      ) : null}
      {data?.unavailable_reason === "account_unavailable" ? (
        <Text as="p">
          The provider account is unavailable. A connection maintainer can check
          its credentials.
        </Text>
      ) : null}
      {snapshot ? (
        <>
          <Text as="p" color="secondary">
            OpenAI Codex{snapshot.plan ? ` · ${snapshot.plan}` : ""} · Last
            successful check {quotaTime(snapshot.fetched_at)}
            {data?.stale
              ? " · Stale observation; remaining quota may have changed"
              : ""}
          </Text>
          {snapshot.spend_control_reached ? (
            <Text as="p">
              The provider reports that an account spending limit has been
              reached.
            </Text>
          ) : null}
          {snapshot.limit_reached_type ? (
            <Text as="p">
              Provider limit: {snapshot.limit_reached_type.replaceAll("_", " ")}
            </Text>
          ) : null}
          {snapshot.buckets.length ? (
            <VStack
              gap={4}

              role="region"
              tabIndex={0}
              aria-label="Provider quota windows"
            >
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Quota bucket</TableHeaderCell>
                    <TableHeaderCell>Window</TableHeaderCell>
                    <TableHeaderCell>Remaining</TableHeaderCell>
                    <TableHeaderCell>Resets</TableHeaderCell>
                    <TableHeaderCell>Provider status</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {snapshot.buckets.flatMap((bucket) => {
                    const windows = bucket.windows.length
                      ? bucket.windows
                      : [null];
                    return windows.map((window) => (
                      <TableRow
                        key={`${bucket.id}:${window?.kind ?? "unknown"}`}
                      >
                        <TableHeaderCell scope="row">
                          {bucket.name ??
                            (bucket.id === "codex" ? "Codex" : bucket.id)}
                          {bucket.normal_model_slug ? (
                            <Text
                              color="secondary"
                              display="block"
                              type="supporting"
                            >
                              Provider model label: {bucket.normal_model_slug}
                            </Text>
                          ) : null}
                        </TableHeaderCell>
                        <TableCell>
                          {window
                            ? windowDuration(window.duration_seconds)
                            : "Unknown"}
                          {window ? (
                            <Text
                              color="secondary"
                              display="block"
                              type="supporting"
                            >
                              {window.kind === "primary"
                                ? "Primary"
                                : "Secondary"}
                            </Text>
                          ) : null}
                        </TableCell>
                        <TableCell>
                          {window?.used_percent !== null &&
                          window?.used_percent !== undefined ? (
                            <>
                              {number.format(
                                Math.max(0, 100 - window.used_percent),
                              )}
                              %
                              <Text
                                color="secondary"
                                display="block"
                                type="supporting"
                              >
                                {number.format(window.used_percent)}% used
                              </Text>
                            </>
                          ) : (
                            "Unknown"
                          )}
                        </TableCell>
                        <TableCell>
                          {quotaTime(window?.resets_at ?? null)}
                        </TableCell>
                        <TableCell>
                          {bucket.allowed === true
                            ? "Usage allowed"
                            : bucket.allowed === false
                              ? "Usage paused"
                              : bucket.limit_reached
                                ? "Limit reached"
                                : "Unknown"}
                        </TableCell>
                      </TableRow>
                    ));
                  })}
                </TableBody>
              </Table>
            </VStack>
          ) : (
            <Text as="p">No quota windows were reported by the provider.</Text>
          )}
          <Text as="p">
            Available usage resets:{" "}
            {snapshot.reset_credits_available === null
              ? "Unknown"
              : number.format(snapshot.reset_credits_available)}
            .
          </Text>
          <Text as="p" color="secondary">
            Window lengths, bucket names, and model labels come from the
            provider. A passed reset time needs a new observation to confirm
            recovery. Manual refreshes are limited to once every 30 seconds.
          </Text>
        </>
      ) : null}
      <Text as="p" color="secondary">
        Anthropic API keys do not expose subscription quota through this
        connection. Quota is unknown.
      </Text>
    </VStack>
  );
}
