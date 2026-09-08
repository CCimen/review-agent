import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import type { UseQueryResult } from "@tanstack/react-query";
import { APIError } from "./api";

export function useFilters() {
  const [params, setParams] = useSearchParams();
  const days = [7, 30, 90].includes(Number(params.get("days")))
    ? Number(params.get("days"))
    : 30;
  const update = (values: Record<string, string>) => {
    const next = new URLSearchParams(params);
    next.delete("before_id");
    next.delete("offset");
    for (const [key, value] of Object.entries(values))
      value ? next.set(key, value) : next.delete(key);
    setParams(next);
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
  useEffect(() => {
    if (!done) return;
    const timer = setTimeout(() => setDone(false), 1600);
    return () => clearTimeout(timer);
  }, [done]);
  if (!navigator.clipboard) return <>{children}</>;
  return (
    <button
      type="button"
      className="copy"
      onClick={() => {
        void navigator.clipboard.writeText(value).then(() => setDone(true));
      }}
    >
      {children}
      <span
        className={done ? "copy-hint done" : "copy-hint"}
        aria-hidden="true"
      >
        {done ? "Copied" : "Copy"}
      </span>
      <span className="sr-only">
        {done ? `${label} copied to clipboard` : `Copy ${label}`}
      </span>
    </button>
  );
}

/** A figure the deployment does not record is unknown, never zero. */
export function Unknown({ children = "Not recorded" }: { children?: string }) {
  return <span className="unknown">{children}</span>;
}

export function Stat({
  label,
  value,
  hint,
  attention,
}: {
  label: string;
  value: number | string | null;
  hint?: ReactNode;
  attention?: boolean;
}) {
  const missing = value === null;
  const text =
    typeof value === "number"
      ? number.format(value)
      : (value ?? "Not recorded");
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span
        className={[
          "stat-value",
          missing ? "unknown" : "",
          attention && value ? "attention" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      >
        {text}
      </span>
      {hint ? <span className="stat-hint">{hint}</span> : null}
    </div>
  );
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
    <section className="section">
      <div className="section-heading">
        <div>
          <h2>{title}</h2>
          {description ? <p className="muted">{description}</p> : null}
        </div>
        {actions}
      </div>
      {children}
    </section>
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
    <label className="field">
      Reporting period
      <select value={days} onChange={(event) => change(event.target.value)}>
        <option value="7">Last 7 days</option>
        <option value="30">Last 30 days</option>
        <option value="90">Last 90 days</option>
      </select>
    </label>
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
    <div className="freshness">
      {query.isError ? (
        <div className="notice error" role="alert">
          <p>
            {query.error.message || "Could not connect to Review Agent."}{" "}
            {query.data ? "The last available data is still shown below." : ""}
          </p>
          {query.error instanceof APIError && query.error.status === 401 ? (
            <button onClick={() => window.location.reload()}>
              Reload to sign in
            </button>
          ) : (
            <button onClick={() => void query.refetch()}>Retry</button>
          )}
        </div>
      ) : (
        <span role={query.isPending ? "status" : undefined}>
          {query.isPending
            ? "Loading…"
            : `Updated ${time(new Date(query.dataUpdatedAt).toISOString())}${interval ? ` · refreshes every ${interval} seconds` : ""}`}
        </span>
      )}
    </div>
  );
}

export function Empty({
  title = "No matching activity",
  level = 2,
  children,
}: {
  title?: string;
  level?: 2 | 3;
  children: ReactNode;
}) {
  const Heading = level === 3 ? "h3" : "h2";
  return (
    <div className="empty">
      <Heading>{title}</Heading>
      <p>{children}</p>
    </div>
  );
}
