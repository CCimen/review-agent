import { Button } from "@astryxdesign/core/Button";
import { CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import type { ISODateTimeString } from "@astryxdesign/core/DateTimeInput";
import { DateTimeInput } from "@astryxdesign/core/DateTimeInput";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
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
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useMutation, useQuery } from "@tanstack/react-query";
import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type { AuditEvent, AuditPage } from "./api";
import { APIError, read, write } from "./api";
import type { components } from "./api.generated";
import { ScopedLink as Link, useScope } from "./scope";
import { Empty, Form, Freshness, dateTimeValue, time } from "./ui";

type AuditAccess = components["schemas"]["AuditAccess"];
type AuditAccessRequest = components["schemas"]["AuditAccessRequest"];
const purposes: Record<AuditAccess["purpose"], string> = {
  incident_investigation: "Incident investigation",
  access_review: "Access review",
  support: "Support request",
  routine_review: "Routine review",
  other: "Other reason",
};

const filterFields = [
  "search",
  "action",
  "outcome",
  "actor_id",
  "since",
  "until",
] as const;
const actions: readonly AuditEvent["action"][] = [
  "audit_access_started",
  "audit_access_ended",
  "audit_viewed",
  "audit_exported",
  "team_created",
  "team_updated",
  "member_added",
  "member_updated",
  "member_removed",
  "repository_assigned",
  "repository_transferred",
  "repository_removed",
  "repository_requested",
  "request_approved",
  "request_rejected",
  "request_withdrawn",
  "account_created",
  "registration_updated",
  "email_updated",
  "account_updated",
  "password_changed",
  "signed_in",
  "signed_out",
  "finding_decided",
  "feedback_triaged",
  "run_action",
  "settings_updated",
  "access_updated",
  "provider_login",
  "provider_login_cancelled",
  "provider_logout",
  "connection_created",
  "connection_updated",
  "connection_removed",
];

function localTime(value: string | null): ISODateTimeString | "" {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : dateTimeValue(
        new Date(date.getTime() - date.getTimezoneOffset() * 60_000),
      );
}

function AuditExport({
  filters,
  grant,
  onExpired,
}: {
  filters: string;
  grant: AuditAccess;
  onExpired: () => void;
}) {
  const [format, setFormat] = useState("json");
  const request = useRef<AbortController | null>(null);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const exportPage = useMutation({
    mutationFn: async (before: string | null) => {
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      const params = new URLSearchParams(filters);
      params.set("format", format);
      if (before) params.set("before_id", before);
      const response = await fetch(`/api/audit/export?${params}`, {
        credentials: "same-origin",
        headers: { "X-Audit-Access-ID": grant.id },
        signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) onExpired();
        throw new APIError(
          response.status,
          "The audit export could not be prepared. Check the filters and try again.",
        );
      }
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download =
        /filename="([^"]+)"/.exec(
          response.headers.get("content-disposition") ?? "",
        )?.[1] ??
        `review-agent-audit.${format === "otlp" ? "otlp.json" : format}`;
      document.body.append(link);
      link.click();
      link.remove();
      // Let the browser start reading the blob before releasing its object URL.
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setCount(Number(response.headers.get("x-audit-count")));
      setNextBefore(response.headers.get("x-audit-next-before-id"));
    },
  });
  return (
    <VStack gap={3}>
      <HStack gap={3} wrap="wrap" vAlign="center">
        <Selector
          label={"Export format"}
          options={[
            { value: "json", label: "JSON" },
            { value: "csv", label: "CSV" },
            { value: "jsonl", label: "JSON Lines" },
            { value: "otlp", label: "OpenTelemetry (OTLP JSON)" },
          ]}
          value={format}
          onChange={(value) => {
            setFormat(value);
            setCount(null);
            setNextBefore(null);
            exportPage.reset();
          }}
          isDisabled={exportPage.isPending}
        />
        <Button
          label={String(
            exportPage.isPending
              ? "Preparing export…"
              : "Export matching events",
          )}
          variant="secondary"
          type="submit"

          isDisabled={exportPage.isPending}
          onClick={() => exportPage.mutate(null)}
        />
        {nextBefore ? (
          <Button
            label={"Export next 1,000"}
            variant="secondary"
            type="submit"

            isDisabled={exportPage.isPending}
            onClick={() => exportPage.mutate(nextBefore)}
          />
        ) : null}
      </HStack>
      <Text as="p" color="secondary" role="status">
        {count === null
          ? "Each file contains up to 1,000 matching events, newest first."
          : `${count.toLocaleString()} events downloaded.${nextBefore ? " More events are available in the next file." : " No older matching events remain."}`}
      </Text>
      {exportPage.isError ? (
        <Text as="p" role="alert">
          {exportPage.error.message}
        </Text>
      ) : null}
    </VStack>
  );
}

export function AuditLog({ teamId }: { teamId?: number }) {
  const scope = useScope();
  useEffect(() => {
    if (!teamId) document.title = "Review Agent · Audit log";
  }, [teamId]);
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          {teamId ? (
            <Heading level={2}>Team audit log</Heading>
          ) : (
            <Heading level={1}>Audit log</Heading>
          )}
          <Text as="p">
            {teamId
              ? "Administration events for this team."
              : "Platform administration · All teams."}{" "}
            {scope.current.role === "owner"
              ? "Account, team, and platform changes visible to owners."
              : "Team, repository, and member changes. Privileged account and sensitive platform changes are visible to owners."}
          </Text>
        </VStack>
      </HStack>
      <AuditAccessGate
        key={`${scope.key}:${teamId ?? "all"}`}
        teamId={teamId}
      />
    </>
  );
}

function AuditAccessGate({ teamId }: { teamId?: number }) {
  const [grant, setGrant] = useState<AuditAccess | null>(null);
  const [notice, setNotice] = useState("");
  const [purpose, setPurpose] = useState<AuditAccess["purpose"] | "">("");
  const [reason, setReason] = useState("");
  const accessPath = (path: string) =>
    teamId ? `${path}?team_id=${teamId}` : path;
  const start = useMutation({
    mutationFn: (request: AuditAccessRequest) =>
      write<AuditAccess>(accessPath("/api/audit/access"), "POST", request),
    onSuccess: (value) => {
      setGrant(value);
      setNotice("");
    },
  });
  const end = useMutation({
    mutationFn: (value: AuditAccess) =>
      write(accessPath(`/api/audit/access/${value.id}/end`), "POST"),
    onSuccess: () => {
      setGrant(null);
      setNotice("Audit access ended.");
    },
    onError: (error) => {
      if (error instanceof APIError && [401, 403].includes(error.status))
        setGrant(null);
    },
  });
  const expire = () => {
    setGrant(null);
    setNotice(
      "Audit access expired or your permissions changed. Explain why you need access to continue.",
    );
  };
  useEffect(() => {
    if (!grant) return;
    const timer = window.setTimeout(
      () => {
        setGrant(null);
        setNotice(
          "Audit access expired. Explain why you need access to continue.",
        );
      },
      Math.max(0, new Date(grant.expires_at).getTime() - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [grant]);
  if (grant)
    return (
      <>
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <VStack gap={3}>
            <strong>{purposes[grant.purpose]}</strong>
            <Text as="p">{grant.reason}</Text>
            <Text color="secondary">
              Access ends {time(grant.expires_at)}. Views and exports are
              recorded.
            </Text>
          </VStack>
          <Button
            label={String(
              end.isPending ? "Ending access…" : "End audit access",
            )}
            variant="secondary"
            type="submit"

            isDisabled={end.isPending}
            onClick={() => end.mutate(grant)}
          />
        </HStack>
        {end.isError ? (
          <Text as="p" role="alert">
            {end.error.message}
          </Text>
        ) : null}
        <AuditEvents teamId={teamId} grant={grant} onExpired={expire} />
      </>
    );
  return (
    <VStack
      gap={4}
      as="section"

      aria-labelledby="audit-access-title"
    >
      <Heading level={2} id="audit-access-title">
        Explain why you need access
      </Heading>
      <Text as="p">
        Your purpose and justification will be recorded with each view or
        export. Access lasts 30 minutes for {teamId ? "this team" : "all teams"}
        .
      </Text>
      {notice ? (
        <Text as="p" role="status">
          {notice}
        </Text>
      ) : null}
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          if (purpose) start.mutate({ purpose, reason: reason.trim() });
        }}
      >
        <fieldset disabled={start.isPending}>
          <VStack gap={4}>
            <Selector
              label={"Purpose"}
              options={[
                { value: "", label: "Select a purpose", disabled: true },
                Object.entries(purposes).map(([value, label]) => ({
                  value: value,
                  label: label,
                })),
              ]
                .flat()
                .filter((option) => option != null)}
              isRequired={true}
              value={purpose}
              onChange={(value) =>
                setPurpose(value as AuditAccess["purpose"] | "")
              }
              isDisabled={start.isPending}
            />

            <TextArea
              label={"Justification"}
              isRequired={true}
              maxLength={500}
              rows={4}
              value={reason}
              onChange={(value) => setReason(value.slice(0, 500))}
              placeholder="Describe the incident, request, or review you are investigating."
              {...({
                required: true,
                minLength: 10,
              } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
            />

            <Text as="p" color="secondary" id="audit-reason-hint">
              10-500 characters. Include enough detail to explain this access to
              another administrator.
            </Text>
            <Button
              label={String(
                start.isPending
                  ? "Recording justification…"
                  : "Access audit log",
              )}
              variant="primary"
              type="submit"
              isDisabled={!purpose || reason.trim().length < 10}
            />
          </VStack>
        </fieldset>
        {start.isError ? (
          <Text as="p" role="alert">
            {start.error.message}
          </Text>
        ) : null}
      </Form>
    </VStack>
  );
}

function AuditFilters({
  location,
  change,
}: {
  location: URLSearchParams;
  change: (value: URLSearchParams) => void;
}) {
  const [draft, setDraft] = useState({
    search: location.get("search") ?? "",
    action: location.get("action") ?? "",
    outcome: location.get("outcome") ?? "",
    actor_id: location.get("actor_id") ?? "",
    since: localTime(location.get("since")),
    until: localTime(location.get("until")),
  });
  function update(
    field: Exclude<keyof typeof draft, "since" | "until">,
    value: string,
  ) {
    setDraft((current) => ({ ...current, [field]: value }));
  }
  return (
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        const next = new URLSearchParams(location);
        next.delete("before_id");
        for (const field of filterFields) {
          const value = draft[field].trim();
          if (value)
            next.set(
              field,
              field === "since" || field === "until"
                ? new Date(value).toISOString()
                : value,
            );
          else next.delete(field);
        }
        change(next);
      }}
    >
      <HStack gap={3} wrap="wrap" vAlign="end">
        <TextInput
          label="Search audit events"
          value={draft.search}
          onChange={(value) => update("search", value)}
          hasClear
          width={280}
          placeholder="Actor, subject, or reason"
          {...({
            maxLength: 200,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />
        <Button label="Apply filters" type="submit" />
        {filterFields.some((field) => location.has(field)) ? (
          <Button
            label="Clear filters"
            variant="ghost"
            onClick={() => {
              const next = new URLSearchParams(location);
              for (const field of [...filterFields, "before_id"])
                next.delete(field);
              change(next);
            }}
          />
        ) : null}
      </HStack>
      <Collapsible
        trigger="Filter by action, actor, outcome, or time"
        defaultIsOpen={filterFields
          .slice(1)
          .some((field) => location.has(field))}
      >
        <VStack gap={4}>
          <Grid gap={4} columns={{ minWidth: 240, max: 4, repeat: "fit" }}>
            <Selector
              label="Action"
              value={draft.action}
              onChange={(value) => update("action", value)}
              options={[
                { value: "", label: "All actions" },
                ...actions.map((value) => ({
                  value,
                  label: value.replaceAll("_", " "),
                })),
              ]}
            />
            <Selector
              label="Outcome"
              value={draft.outcome}
              onChange={(value) => update("outcome", value)}
              options={[
                { value: "", label: "All outcomes" },
                { value: "started", label: "Started" },
                { value: "succeeded", label: "Succeeded" },
                { value: "failed", label: "Failed" },
              ]}
            />
            <TextInput
              label="Actor account ID"
              value={draft.actor_id}
              onChange={(value) => update("actor_id", value)}
              placeholder="All actors"
              {...({
                maxLength: 36,
                pattern:
                  "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
              } satisfies InputHTMLAttributes<HTMLInputElement>)}
            />
            <DateTimeInput
              label="From · local time"
              value={draft.since || undefined}
              onChange={(value) =>
                setDraft((current) => ({ ...current, since: value ?? "" }))
              }
              hasClear
              hourFormat="24h"
            />
            <DateTimeInput
              label="Until · local time"
              value={draft.until || undefined}
              onChange={(value) =>
                setDraft((current) => ({ ...current, until: value ?? "" }))
              }
              hasClear
              hourFormat="24h"
            />
          </Grid>
          <Text color="secondary">
            Search matches all supplied words. The start is included; the end is
            excluded.
          </Text>
        </VStack>
      </Collapsible>
    </Form>
  );
}

function AuditEvents({
  teamId,
  grant,
  onExpired,
}: {
  teamId?: number;
  grant: AuditAccess;
  onExpired: () => void;
}) {
  const scope = useScope();
  const [location, setLocation] = useSearchParams();
  const before = location.get("before_id");
  const filters = new URLSearchParams();
  for (const field of filterFields) {
    const value = location.get(field);
    if (value) filters.set(field, value);
  }
  if (teamId) filters.set("team_id", String(teamId));
  const filterKey = filters.toString();
  const params = new URLSearchParams(filters);
  if (before) params.set("before_id", before);
  const setPage = (value: number | null) => {
    const next = new URLSearchParams(location);
    if (value) next.set("before_id", String(value));
    else next.delete("before_id");
    setLocation(next);
  };
  const query = useQuery({
    queryKey: [
      "audit",
      grant.id,
      teamId,
      params.toString(),
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<AuditPage>(`/api/audit?${params}`, signal, {
        "X-Audit-Access-ID": grant.id,
      }),
    gcTime: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  useEffect(() => {
    if (
      query.error instanceof APIError &&
      [401, 403].includes(query.error.status)
    )
      onExpired();
  }, [query.error, onExpired]);
  return (
    <>
      <AuditFilters key={filterKey} location={location} change={setLocation} />
      <AuditExport
        key={filterKey}
        filters={filterKey}
        grant={grant}
        onExpired={onExpired}
      />
      <Freshness query={query} />
      {query.data?.items.length ? (
        <VStack
          gap={4}

          tabIndex={0}
          role="region"
          aria-label="Audit events"
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>When / action</TableHeaderCell>
                <TableHeaderCell>Actor</TableHeaderCell>
                <TableHeaderCell>Subject / reason</TableHeaderCell>
                <TableHeaderCell>Details</TableHeaderCell>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.map((event) => (
                <TableRow key={event.id}>
                  <TableHeaderCell scope="row">
                    <Text>{event.action.replaceAll("_", " ")}</Text>
                    <Text color="secondary" display="block" type="supporting">
                      {time(event.recorded_at)}
                    </Text>
                    {event.outcome !== "succeeded" ? (
                      <Text>
                        {event.outcome === "started"
                          ? "Started · outcome not yet recorded"
                          : "Failed"}
                      </Text>
                    ) : null}
                  </TableHeaderCell>
                  <TableCell>
                    {event.actor_email ?? "System"}
                    <Text color="secondary" display="block" type="supporting">
                      {event.actor_role}
                    </Text>
                    <Text color="secondary" display="block" type="supporting">
                      {event.actor_id}
                    </Text>
                  </TableCell>
                  <TableCell>
                    <Text>{event.subject}</Text>
                    <Text as="p">{event.reason}</Text>
                    {event.team_id && !teamId ? (
                      <Link
                        to={`/teams/${event.team_id}?team_id=${event.team_id}&tab=audit`}
                      >
                        Team #{event.team_id}
                      </Link>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <AuditJSON event={event} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </VStack>
      ) : query.data ? (
        <Empty title="No audit events in this view">
          Changes appear here after they are recorded. Try clearing the filters
          or returning to the latest events.
        </Empty>
      ) : null}
      {before || query.data?.next_before_id ? (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"Latest events"}
            variant="secondary"
            type="submit"

            isDisabled={!before}
            onClick={() => setPage(null)}
          />
          <Button
            label={"Older events"}
            variant="secondary"
            type="submit"

            isDisabled={!query.data?.next_before_id}
            onClick={() => setPage(query.data?.next_before_id ?? null)}
          />
        </HStack>
      ) : null}
    </>
  );
}

export function AuditJSON({ event }: { event: AuditEvent }) {
  const json = JSON.stringify(event, null, 2);
  const [copyStatus, setCopyStatus] = useState("");
  return (
    <Collapsible
      defaultIsOpen={false}
      trigger={
        <HStack gap={3} wrap="wrap" vAlign="center">
          View JSON · #{event.id}
        </HStack>
      }
    >
      <VStack gap={4}>
        <HStack gap={3} wrap="wrap" vAlign="center">
          <Button
            label={"Copy JSON"}
            variant="primary"
            type="button"

            onClick={() => {
              if (!navigator.clipboard) {
                setCopyStatus("Select the JSON below to copy it.");
                return;
              }
              void navigator.clipboard.writeText(json).then(
                () => setCopyStatus("JSON copied."),
                () =>
                  setCopyStatus(
                    "Copy failed. Select the JSON below to copy it.",
                  ),
              );
            }}
          />
          <Text color="secondary" role="status">
            {copyStatus}
          </Text>
        </HStack>
        <CodeBlock
          code={json}
          language="json"
          width="100%"
          aria-label={`JSON for audit event ${event.id}`}
        />
      </VStack>
    </Collapsible>
  );
}
