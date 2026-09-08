import type { components } from "./api.generated";

export type RepositoryPage = components["schemas"]["RepositoryPage"];
export type PullRequestGroup = components["schemas"]["PullRequestGroup"];
export type PullRequestPage = components["schemas"]["PullRequestPage"];
export type HistoryPage = components["schemas"]["HistoryPage"];
export type HistoryItem = components["schemas"]["HistoryItem"];
export type ReviewDetail = components["schemas"]["ReviewDetail"];
export type AccountPage = components["schemas"]["AccountPage"];
export type Overview = components["schemas"]["Overview"];
export type ActivityCounts = components["schemas"]["ActivityCounts"];
export type ActivityDay = components["schemas"]["ActivityDay"];
export type FailureCount = components["schemas"]["FailureCount"];
export type Operations = components["schemas"]["Operations"];
export type QueueStatus = components["schemas"]["QueueStatus"];
export type WorkerInstance = components["schemas"]["WorkerInstance"];
export type WorkerEvent = components["schemas"]["WorkerEvent"];
export type WorkerEventPage = components["schemas"]["WorkerEventPage"];
export type Account = components["schemas"]["Account"];
export type NewAccount = components["schemas"]["NewAccount"];
export type AccountUpdate = components["schemas"]["AccountUpdate"];
export type PasswordChange = components["schemas"]["PasswordChange"];

export type Team = components["schemas"]["Team"];
export type TeamPage = components["schemas"]["TeamPage"];
export type TeamMember = components["schemas"]["TeamMember"];
export type TeamMemberPage = components["schemas"]["TeamMemberPage"];
export type AuditPage = components["schemas"]["AuditPage"];
export type AuditEvent = components["schemas"]["AuditEvent"];
export type RepositoryRequest = components["schemas"]["RepositoryRequest"];
export type RepositoryRequestPage =
  components["schemas"]["RepositoryRequestPage"];
export type TeamRepositoryPage = components["schemas"]["TeamRepositoryPage"];

export class APIError extends Error {
  constructor(
    public status: number,
    message?: string,
  ) {
    super(
      message ??
        (status === 401
          ? "Sign in to continue."
          : status === 422
            ? "These filters are invalid. Reset the filters and try again."
            : "Review data is unavailable. Retry shortly."),
    );
  }
}

export async function write<T = void>(
  path: string,
  method: "POST" | "PATCH" | "PUT",
  body?: unknown,
): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const error: unknown = await response.json().catch(() => null);
    const message =
      error &&
      typeof error === "object" &&
      "detail" in error &&
      typeof error.detail === "string"
        ? error.detail
        : "Check the submitted fields and try again.";
    throw new APIError(response.status, message);
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}

export async function login(email: string, password: string): Promise<void> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username: email, password }),
  });
  if (!response.ok)
    throw new APIError(
      response.status,
      response.status === 400
        ? "Email or password is incorrect, or this account is disabled."
        : "Sign-in is unavailable. Please try again.",
    );
}

export async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    signal,
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new APIError(response.status);
  return response.json() as Promise<T>;
}
