import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import type { ISODateTimeString } from "@astryxdesign/core/DateTimeInput";
import { EmptyState } from "@astryxdesign/core/EmptyState";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Skeleton } from "@astryxdesign/core/Skeleton";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Heading, Text } from "@astryxdesign/core/Text";
import type { UseQueryResult } from "@tanstack/react-query";
import type { ComponentProps, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { APIError } from "./api";

export function dateTimeValue(date: Date): ISODateTimeString {
  return date.toISOString().slice(0, 16) as ISODateTimeString;
}

/** Astryx's numeric editor exposes invalid drafts through aria-invalid.
 * Keep those drafts from submitting the previously committed value. */
export function Form({ children, onSubmit, ...props }: ComponentProps<"form">) {
  return (
    <form
      {...props}
      onSubmit={(event) => {
        const invalid = event.currentTarget.querySelector<HTMLElement>(
          '[aria-invalid="true"]',
        );
        if (invalid) {
          event.preventDefault();
          invalid.focus();
          return;
        }
        onSubmit?.(event);
      }}
    >
      <VStack gap={4}>{children}</VStack>
    </form>
  );
}

/** A filter that applies as the reader types, rather than waiting for a button
 *  they have to find. The URL stays the source of truth, so the field keeps a
 *  local draft until typing settles; `apply` should replace the history entry
 *  so one search does not fill the back button with every keystroke.
 *
 *  The draft follows the URL when it changes elsewhere — a link, the back
 *  button — but not when it changes because this field just applied, which
 *  would strip a space the reader had typed in the meantime. */
export function useLiveSearch(
  current: string,
  apply: (value: string) => void,
  delay = 250,
) {
  const [draft, setDraft] = useState(current);
  const latest = useRef(apply);
  latest.current = apply;
  const applied = useRef(current);
  useEffect(() => {
    if (current === applied.current) return;
    applied.current = current;
    setDraft(current);
  }, [current]);
  useEffect(() => {
    const value = draft.trim();
    if (value === current) return;
    const timer = setTimeout(() => {
      applied.current = value;
      latest.current(value);
    }, delay);
    return () => clearTimeout(timer);
  }, [draft, current, delay]);
  return {
    draft,
    setDraft,
    /** Apply now, for a reader who presses Enter rather than waiting. */
    flush: () => {
      const value = draft.trim();
      if (value === current) return;
      applied.current = value;
      apply(value);
    },
  };
}

export function useFilters() {
  const [params, setParams] = useSearchParams();
  const days = [7, 30, 90].includes(Number(params.get("days")))
    ? Number(params.get("days"))
    : 30;
  const update = (
    values: Record<string, string>,
    options?: { replace?: boolean },
  ) => {
    const next = new URLSearchParams(params);
    next.delete("before_id");
    next.delete("offset");
    for (const [key, value] of Object.entries(values))
      value ? next.set(key, value) : next.delete(key);
    setParams(next, options);
  };
  return { params, days, update };
}

export const number = new Intl.NumberFormat();
const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
const dayOnly = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
});

export const time = (value: string | null | undefined) =>
  value ? dateTime.format(new Date(value)) : "—";

export const day = (value: string) => dayOnly.format(new Date(value));

/** Whole seconds read as noise on a sub-second measurement and as false
 *  precision on a long one, so the unit follows the magnitude. */
export function duration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return null;
  if (seconds < 1) return `${seconds.toFixed(2)} s`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
}

/** How long ago, for heartbeats and queue ages where the absolute timestamp
 *  matters less than the gap. */
export function since(value: string | null | undefined, now = Date.now()) {
  if (!value) return null;
  const seconds = Math.max(0, Math.round((now - Date.parse(value)) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds} seconds ago`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  }
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  }
  const days = Math.floor(seconds / 86400);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** Recorded causes are stored as tokens. Operators still need the token to
 *  grep logs, but "review_failed" is not an answer to "why did this fail?". */
const failureSentences: Record<string, string> = {
  review_failed: "The review did not finish",
  publication_failed: "The result never reached GitHub",
  snapshot_changed: "The pull request changed while under review",
  snapshot_superseded: "A newer request replaced this one",
  authorization_failed: "The requester was not authorized",
  timeout: "The review ran out of time",
  provider_error: "The model provider returned an error",
  cancelled: "The request was cancelled",
};
export const failureSentence = (code: string) =>
  code
    ? (failureSentences[code] ?? code.replaceAll("_", " "))
    : "No cause recorded";

/** Request IDs, commit SHAs and worker IDs exist to be pasted into another
 *  tool. Selecting a wrapped <code> by hand is the friction this removes. */
export function Copy({
  value,
  children,
  label,
}: {
  value: string;
  children: ReactNode;
  label: string;
}) {
  const [done, setDone] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!done) return;
    const timer = setTimeout(() => setDone(false), 1600);
    return () => clearTimeout(timer);
  }, [done]);
  if (typeof navigator === "undefined" || !navigator.clipboard)
    return <>{children}</>;
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        label={done ? `${label} copied to clipboard` : `Copy ${label}`}
        onClick={() => {
          setFailed(false);
          void navigator.clipboard
            .writeText(value)
            .then(() => setDone(true))
            .catch(() => setFailed(true));
        }}
      >
        <HStack as="span" gap={2} vAlign="center">
          {children}
          <Text aria-hidden="true">{done ? "Copied" : "Copy"}</Text>
        </HStack>
      </Button>
      {failed && (
        <Text as="span" role="status">
          {" "}
          Could not copy. Select the value to copy it manually.
        </Text>
      )}
    </>
  );
}

/** A figure the deployment does not record is unknown, never zero. */
export function Unknown({ children = "Not recorded" }: { children?: string }) {
  return <Text color="secondary">{children}</Text>;
}

export function Stat({
  label,
  value,
  hint,
  attention,
}: {
  label: string;
  /** `null` states that the deployment holds no record of this figure.
   *  `undefined` means the answer has not arrived yet, which is a
   *  different claim and must not be printed as "Not recorded". */
  value: number | string | null | undefined;
  hint?: ReactNode;
  attention?: boolean;
}) {
  const pending = value === undefined;
  const missing = value === null;
  const text =
    typeof value === "number"
      ? number.format(value)
      : (value ?? "Not recorded");
  return (
    <VStack gap={2}>
      <HStack gap={2} vAlign="center">
        {attention && value ? (
          <StatusDot
            variant="error"
            label="Needs attention"
            aria-hidden="true"
          />
        ) : null}
        <Text color="secondary">{label}</Text>
      </HStack>
      {pending ? (
        // The shimmer occupies the line box the figure will take, so the
        // tile does not shift when the answer lands.
        <HStack height={34} vAlign="center" aria-label={`${label}, loading`}>
          <Skeleton width={72} height={24} radius={2} />
        </HStack>
      ) : (
        <Text
          size="2xl"
          weight="semibold"
          color={missing ? "secondary" : "primary"}
        >
          {text}
        </Text>
      )}
      {hint ? <Text type="supporting">{hint}</Text> : null}
    </VStack>
  );
}

/** Explanatory prose sits inside a readable measure. The console body runs to
 *  1180 CSS pixels on a wide screen, which is roughly twice a comfortable line
 *  length, so notes that span it become hard to track back to the next line. */
export function Prose({ children }: { children: ReactNode }) {
  return <VStack maxWidth={620}>{children}</VStack>;
}

export function Section({
  title,
  description,
  children,
  actions,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <VStack as="section" gap={4}>
      <HStack gap={4} hAlign="between" vAlign="start" wrap="wrap">
        <VStack gap={1}>
          <Heading level={2}>{title}</Heading>
          {description ? <Text color="secondary">{description}</Text> : null}
        </VStack>
        {actions}
      </HStack>
      {children}
    </VStack>
  );
}

export function Period({
  days,
  change,
}: {
  days: number;
  change: (value: string) => void;
}) {
  return (
    <Selector
      label="Reporting period"
      value={String(days)}
      onChange={change}
      options={[
        { value: "7", label: "Last 7 days" },
        { value: "30", label: "Last 30 days" },
        { value: "90", label: "Last 90 days" },
      ]}
    />
  );
}

export function Freshness<T>({
  query,
  interval = 10,
  quiet,
}: {
  query: UseQueryResult<T, Error>;
  interval?: number | false;
  quiet?: boolean;
}) {
  if (quiet && !query.isError && !query.isPending) return null;
  return (
    <VStack gap={2}>
      {query.isError ? (
        <Banner
          status="error"
          title={query.error.message || "Could not connect to Review Agent."}
          description={
            query.data
              ? "The last available data is still shown below."
              : undefined
          }
          endContent={
            query.error instanceof APIError && query.error.status === 401 ? (
              <Button
                label="Reload to sign in"
                onClick={() => window.location.reload()}
              />
            ) : (
              <Button label="Retry" onClick={() => void query.refetch()} />
            )
          }
        />
      ) : (
        <Text type="supporting" role={query.isPending ? "status" : undefined}>
          {query.isPending
            ? "Loading…"
            : `Updated ${time(new Date(query.dataUpdatedAt).toISOString())}${interval ? ` · refreshes every ${interval} seconds` : ""}`}
        </Text>
      )}
    </VStack>
  );
}

export function Empty({
  title = "Nothing to show",
  level = 2,
  children,
}: {
  title?: string;
  level?: 2 | 3;
  children: ReactNode;
}) {
  return (
    <VStack gap={3} role="status" maxWidth={560} hAlign="center" width="100%">
      <EmptyState
        title={title}
        headingLevel={level}
        isCompact
        description={typeof children === "string" ? children : undefined}
      />
      {typeof children !== "string" ? (
        <Text color="secondary">{children}</Text>
      ) : null}
    </VStack>
  );
}
