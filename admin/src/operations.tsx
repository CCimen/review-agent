import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { read } from "./api";
import type {
  Operations,
  QueueStatus,
  WorkerEventPage,
  WorkerInstance,
} from "./api";
import {
  Copy,
  Empty,
  Freshness,
  Section,
  Stat,
  number,
  since,
  time,
} from "./ui";

const workerStates: Record<WorkerInstance["state"], string> = {
  running: "Running",
  draining: "Draining",
  stopped: "Stopped",
  unresponsive: "Unresponsive",
};
/** Reuses the review-state badge palette: running is informational, draining
 *  and unresponsive want attention, stopped is inert. */
const workerTone: Record<WorkerInstance["state"], string> = {
  running: "running",
  draining: "queued",
  stopped: "",
  unresponsive: "failed",
};
/** What an operator scans for is the role; the instance id is what they copy. */
const workerRoles: Record<string, string> = {
  review: "Review worker",
  webhook: "Webhook worker",
  publisher: "Publisher",
};
const queueLabels: Record<string, string> = {
  review: "Review queue",
  publisher: "Publication queue",
  webhook: "Webhook queue",
};
/** Work flows webhook to review to publication; the API orders queues by name. */
const queueOrder = ["webhook", "review", "publisher"];
const byPipeline = (a: QueueStatus, b: QueueStatus) =>
  queueOrder.indexOf(a.kind) - queueOrder.indexOf(b.kind);

function Workers({
  workers,
  truncated,
  staleAfter,
  onInspect,
}: {
  workers: WorkerInstance[];
  truncated: boolean;
  staleAfter: number;
  onInspect: (id: string) => void;
}) {
  if (!workers.length)
    return (
      <Empty title="No workers are reporting" level={3}>
        No process has sent a heartbeat to this database. Workers report only
        after the schema and worker image are both upgraded, so this does not
        prove that no worker is running. Check your container platform.
      </Empty>
    );
  return (
    <div className="panel">
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label="Workers"
      >
        <table className="ops-table">
          <thead>
            <tr>
              <th scope="col">Worker</th>
              <th scope="col">State</th>
              <th scope="col" className="numeric">
                Capacity
              </th>
              <th scope="col" className="numeric">
                Active leases
              </th>
              <th scope="col">Last heartbeat</th>
              <th scope="col">Events</th>
            </tr>
          </thead>
          <tbody>
            {workers.map((worker) => (
              <tr key={worker.id}>
                <th scope="row">
                  <span className="worker-role">
                    {workerRoles[worker.kind] ?? worker.kind}
                  </span>
                  <span className="subtext">
                    <Copy value={worker.id} label="worker ID">
                      <code>{worker.id}</code>
                    </Copy>
                  </span>
                  <span className="subtext">
                    lease owner {worker.lease_owner} · started{" "}
                    {time(worker.started_at)}
                  </span>
                </th>
                <td>
                  <span className="mobile-label" aria-hidden="true">
                    State
                  </span>
                  <span className={`status ${workerTone[worker.state]}`}>
                    {workerStates[worker.state]}
                  </span>
                </td>
                <td className="numeric">
                  <span className="mobile-label" aria-hidden="true">
                    Capacity
                  </span>
                  {number.format(worker.capacity)}
                </td>
                <td className="numeric">
                  <span className="mobile-label" aria-hidden="true">
                    Active leases
                  </span>
                  {worker.active_leases === null ? (
                    <span
                      className="unknown"
                      title="Several retained processes share this lease owner"
                    >
                      Shared
                    </span>
                  ) : (
                    number.format(worker.active_leases)
                  )}
                </td>
                <td>
                  <span className="mobile-label" aria-hidden="true">
                    Last heartbeat
                  </span>
                  {since(worker.last_seen_at)}
                  <span className="subtext">{time(worker.last_seen_at)}</span>
                </td>
                <td>
                  <button
                    className="text-button"
                    onClick={() => onInspect(worker.id)}
                  >
                    View
                    <span className="sr-only">{` events for ${worker.id}`}</span>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated && (
        <p className="panel-note">
          Only the first 100 instances are listed. Older stopped processes are
          removed after seven days without a heartbeat.
        </p>
      )}
      <p className="panel-note">
        A worker is marked unresponsive after {staleAfter} seconds without a
        heartbeat. That means this database stopped hearing from it, not that
        its container failed.
      </p>
    </div>
  );
}

function Queue({
  queue,
  consumers,
}: {
  queue: QueueStatus;
  consumers: number | null;
}) {
  const stuck = queue.expired_leases > 0 || queue.failed > 0;
  const backlog = queue.due > 0;
  // An empty queue means "drained" only if something is running to drain it.
  // With no live worker the honest reading is that we cannot tell.
  const tone =
    consumers === null || consumers === 0
      ? "unknown"
      : backlog
        ? "queued"
        : "published";
  const label =
    consumers === null
      ? "Worker list incomplete"
      : consumers === 0
        ? backlog
          ? `${number.format(queue.due)} due · no workers`
          : "No workers reporting"
        : backlog
          ? `${number.format(queue.due)} due`
          : queue.waiting
            ? "Scheduled"
            : "Clear";
  return (
    <div className={stuck ? "queue panel attention-panel" : "queue panel"}>
      <div className="queue-head">
        <h3>{queueLabels[queue.kind] ?? queue.kind}</h3>
        <span className={`status ${tone}`}>{label}</span>
      </div>
      <dl>
        <div>
          <dt>Waiting</dt>
          <dd>{number.format(queue.waiting)}</dd>
        </div>
        <div>
          <dt>Due now</dt>
          <dd>{number.format(queue.due)}</dd>
        </div>
        <div>
          <dt>Delayed</dt>
          <dd>{number.format(queue.delayed)}</dd>
        </div>
        <div>
          <dt>Leased</dt>
          <dd>{number.format(queue.leased)}</dd>
        </div>
        <div>
          <dt>Expired leases</dt>
          <dd className={queue.expired_leases ? "attention" : undefined}>
            {number.format(queue.expired_leases)}
          </dd>
        </div>
        <div>
          <dt>Failed</dt>
          <dd className={queue.failed ? "attention" : undefined}>
            {number.format(queue.failed)}
          </dd>
        </div>
      </dl>
      <p className="field-help">
        {queue.oldest_waiting_at
          ? `Oldest waiting since ${time(queue.oldest_waiting_at)}.`
          : "Nothing waiting."}
        {queue.next_available_at
          ? ` Next becomes available ${time(queue.next_available_at)}.`
          : ""}
      </p>
    </div>
  );
}

function Events({ workerId, clear }: { workerId: string; clear: () => void }) {
  const [cursor, setCursor] = useState<number | null>(null);
  useEffect(() => {
    setCursor(null);
  }, [workerId]);
  const params = new URLSearchParams({ limit: "50" });
  if (workerId) params.set("worker_id", workerId);
  if (cursor !== null) params.set("before_id", String(cursor));
  const query = useQuery({
    queryKey: ["events", params.toString()],
    queryFn: ({ signal }) =>
      read<WorkerEventPage>(`/api/operations/events?${params}`, signal),
  });
  return (
    <>
      {workerId && (
        <p className="result-count">
          Showing events for <code>{workerId}</code>.{" "}
          <button className="text-button inline" onClick={clear}>
            Show all workers
          </button>
        </p>
      )}
      <Freshness query={query} quiet />
      {query.data &&
        (query.data.items.length ? (
          <div className="panel event-list">
            {query.data.items.map((event) => (
              <div className="event" key={event.id}>
                <time dateTime={event.occurred_at}>
                  {time(event.occurred_at)}
                </time>
                <code className="event-name">{event.event}</code>
                <span className="event-worker">
                  <code>{event.worker_id}</code>
                </span>
                <span className="event-refs">
                  {event.review_run_id !== null && `run ${event.review_run_id}`}
                  {event.review_run_id !== null &&
                    event.job_id !== null &&
                    " · "}
                  {event.job_id !== null && `job ${event.job_id}`}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <Empty title="No events recorded" level={3}>
            {workerId
              ? "This worker has recorded no events yet."
              : "Workers record process lifecycle and review handoff here once the upgraded worker image is running."}
          </Empty>
        ))}
      {query.data && (cursor !== null || query.data.next_cursor !== null) && (
        <div className="pagination">
          <button
            className="secondary"
            disabled={cursor === null}
            onClick={() => setCursor(null)}
          >
            Newest events
          </button>
          <span>Newest first · up to 50 per page</span>
          <button
            className="secondary"
            disabled={query.data.next_cursor === null}
            onClick={() => setCursor(query.data?.next_cursor ?? null)}
          >
            Older events
          </button>
        </div>
      )}
    </>
  );
}

export function OperationsPage() {
  const [workerId, setWorkerId] = useState("");
  useEffect(() => {
    document.title = "Review Agent · Operations";
  }, []);
  const query = useQuery({
    queryKey: ["operations"],
    queryFn: ({ signal }) => read<Operations>("/api/operations", signal),
  });
  const data = query.data;
  const running =
    data?.workers.filter((worker) => worker.state === "running").length ?? 0;
  const unresponsive =
    data?.workers.filter((worker) => worker.state === "unresponsive").length ??
    0;
  const due = data?.queues.reduce((sum, queue) => sum + queue.due, 0) ?? 0;
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Operations</h1>
          <p>Worker presence and queue diagnostics for this deployment.</p>
        </div>
      </div>
      <Freshness query={query} />
      {data && (
        <>
          <div className="stat-grid live">
            <Stat
              label="Workers running"
              value={running}
              hint={
                data.workers_truncated ? "Among shown instances" : undefined
              }
            />
            <Stat
              label="Unresponsive"
              value={unresponsive}
              hint={
                data.workers_truncated ? "Among shown instances" : undefined
              }
              attention
            />
            <Stat label="Work due now" value={due} />
            <Stat
              label="Reporting instances"
              value={data.workers.length}
              hint={data.workers_truncated ? "First 100 shown" : undefined}
            />
          </div>

          <Section
            title="Workers"
            description="Processes that have reported to this database."
          >
            <Workers
              workers={data.workers}
              truncated={data.workers_truncated}
              staleAfter={data.stale_after_seconds}
              onInspect={setWorkerId}
            />
          </Section>

          <Section
            title="Queues"
            description="Work waiting to be claimed. Due means the availability deadline has passed; repository scheduling and authorization can still delay a claim."
          >
            <div className="queue-grid">
              {[...data.queues].sort(byPipeline).map((queue) => {
                const consumers = data.workers.filter(
                  (worker) =>
                    worker.kind === queue.kind && worker.state === "running",
                ).length;
                return (
                  <Queue
                    key={queue.kind}
                    queue={queue}
                    consumers={
                      consumers === 0 && data.workers_truncated
                        ? null
                        : consumers
                    }
                  />
                );
              })}
            </div>
            <p className="stat-note">
              {running === 0 && !data.workers_truncated
                ? "No worker is reporting, so an empty queue does not mean work is being drained. Provider cooldowns held outside these queues are not shown here."
                : "Provider cooldowns that are not stored in these queues are not shown here."}
            </p>
          </Section>

          <Section
            title="Events"
            description="Process lifecycle and review handoff. These records carry no output, prompts, or provider responses — use your container platform for full logs."
          >
            <Events workerId={workerId} clear={() => setWorkerId("")} />
          </Section>
        </>
      )}
    </>
  );
}
