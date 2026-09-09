import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { StatusDot } from "@astryxdesign/core/StatusDot";
import { Heading, Text } from "@astryxdesign/core/Text";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { GitHubConnection } from "./access";
import type {
  Operations,
  QueueStatus,
  WorkerEventPage,
  WorkerInstance,
} from "./api";
import { read } from "./api";
import { EngineServices, HermesHealth, ProviderHealth } from "./deployment";
import { useScope } from "./scope";
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
  running: "Online",
  draining: "Draining",
  stopped: "Stopped",
  unresponsive: "Unresponsive",
};
type Tone = "success" | "warning" | "error" | "accent" | "neutral";
const workerTone: Record<WorkerInstance["state"], Tone> = {
  running: "success",
  draining: "warning",
  stopped: "neutral",
  unresponsive: "error",
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
        Worker status is unavailable until a worker reports to this database.
        Check the worker services in your deployment platform and confirm that
        they use this deployment’s database.
      </Empty>
    );
  return (
    <VStack gap={4}>
      <VStack
        gap={0}

        tabIndex={0}
        role="region"
        aria-label="Workers"
      >
        <Table>
          <TableHeader>
            <TableRow>
              <TableHeaderCell scope="col">Worker</TableHeaderCell>
              <TableHeaderCell scope="col">Availability</TableHeaderCell>
              <TableHeaderCell scope="col">Capacity</TableHeaderCell>
              <TableHeaderCell scope="col">Assigned jobs</TableHeaderCell>
              <TableHeaderCell scope="col">Last heartbeat</TableHeaderCell>
              <TableHeaderCell scope="col">Events</TableHeaderCell>
            </TableRow>
          </TableHeader>
          <TableBody>
            {workers.map((worker) => (
              <TableRow key={worker.id}>
                <TableHeaderCell scope="row">
                  <Text>{workerRoles[worker.kind] ?? worker.kind}</Text>
                  <Text color="secondary" display="block" type="supporting">
                    <Copy value={worker.id} label="worker ID">
                      <Code>{worker.id}</Code>
                    </Copy>
                  </Text>
                  <Text color="secondary" display="block" type="supporting">
                    lease owner {worker.lease_owner} · started{" "}
                    {time(worker.started_at)}
                  </Text>
                </TableHeaderCell>
                <TableCell>
                  <HStack gap={2} vAlign="center">
                    <StatusDot
                      aria-hidden="true"
                      label={workerStates[worker.state]}
                      variant={workerTone[worker.state]}
                    />
                    <Text>{workerStates[worker.state]}</Text>
                  </HStack>
                </TableCell>
                <TableCell>{number.format(worker.capacity)}</TableCell>
                <TableCell>
                  {worker.active_leases === null ? (
                    <abbr title="Several retained processes share this lease owner">
                      <Text color="secondary">Unknown</Text>
                    </abbr>
                  ) : (
                    number.format(worker.active_leases)
                  )}
                  {worker.state === "running" && worker.active_leases === 0 && (
                    <Text color="secondary" display="block" type="supporting">
                      Idle
                    </Text>
                  )}
                </TableCell>
                <TableCell>
                  {since(worker.last_seen_at)}
                  <Text color="secondary" display="block" type="supporting">
                    {time(worker.last_seen_at)}
                  </Text>
                </TableCell>
                <TableCell>
                  <AstryxLink
                    href="#worker-events"
                    onClick={() => onInspect(worker.id)}
                  >
                    View events
                    <VisuallyHidden>{` for ${worker.id}`}</VisuallyHidden>
                  </AstryxLink>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </VStack>
      {truncated && (
        <Text as="p" color="secondary">
          Only the first 100 instances are listed. Older stopped processes are
          removed after seven days without a heartbeat.
        </Text>
      )}
      <Text as="p" color="secondary">
        Online means the worker is sending heartbeats. An online worker with
        zero assigned jobs is idle. Capacity is its maximum concurrent job
        count. A worker is marked unresponsive after {staleAfter} seconds
        without a heartbeat; this does not confirm that its container stopped.
      </Text>
    </VStack>
  );
}

function Queue({ queue }: { queue: QueueStatus }) {
  const backlog = queue.due > 0;
  // An idle queue has nothing to report but still filled a card with six
  // zeros. The counts appear once any of them carries information.
  const counts = [
    queue.waiting,
    queue.due,
    queue.delayed,
    queue.leased,
    queue.expired_leases,
    queue.failed,
  ];
  const idle = counts.every((count) => count === 0);
  const hasLiveWorker = queue.live_workers > 0;
  const tone =
    queue.expired_leases > 0
      ? "error"
      : backlog
        ? "warning"
        : !hasLiveWorker
          ? "neutral"
          : "success";
  const label =
    queue.expired_leases > 0
      ? "Recovery needed"
      : !hasLiveWorker
        ? "No live worker"
        : backlog
          ? "Ready"
          : queue.leased > 0
            ? "In progress"
            : queue.waiting
              ? "Scheduled"
              : "Clear";
  return (
    <VStack gap={3}>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <Heading level={3}>{queueLabels[queue.kind] ?? queue.kind}</Heading>
        <HStack gap={2} vAlign="center">
          <StatusDot aria-hidden="true" label={label} variant={tone} />
          <Text>{label}</Text>
        </HStack>
      </HStack>
      {idle ? null : (
        /* The design system owns this key/value pairing; the hand-built
           definition list set its own gaps and crowded the six counts. */
        <MetadataList label={{ position: "start" }}>
          <MetadataListItem label="Waiting">
            {number.format(queue.waiting)}
          </MetadataListItem>
          <MetadataListItem label="Ready to start">
            {number.format(queue.due)}
          </MetadataListItem>
          <MetadataListItem label="Delayed">
            {number.format(queue.delayed)}
          </MetadataListItem>
          <MetadataListItem label="Claimed by workers">
            {number.format(queue.leased)}
          </MetadataListItem>
          <MetadataListItem label="Expired claims">
            {number.format(queue.expired_leases)}
          </MetadataListItem>
          <MetadataListItem label="Retained failures">
            {number.format(queue.failed)}
          </MetadataListItem>
        </MetadataList>
      )}
      {Number.isFinite(queue.live_workers) &&
      Number.isFinite(queue.worker_capacity) ? (
        <Text type="supporting">
          {number.format(queue.live_workers)} live workers ·{" "}
          {number.format(queue.worker_capacity)} slots
        </Text>
      ) : null}
      {queue.capacity_waiting > 0 && (
        <Text type="supporting">
          Waiting for review capacity: {number.format(queue.capacity_waiting)}
        </Text>
      )}
      {queue.cooldown_waiting > 0 && (
        <Text type="supporting">
          Waiting for account quota: {number.format(queue.cooldown_waiting)}
          {queue.cooldown_until
            ? ` · next check ${time(queue.cooldown_until)}`
            : ""}
        </Text>
      )}
      {queue.oldest_due_at && (
        <Text type="supporting">
          Oldest due since {time(queue.oldest_due_at)}
        </Text>
      )}
      <Text as="p" color="secondary">
        {queue.oldest_waiting_at
          ? `Oldest waiting since ${time(queue.oldest_waiting_at)}.`
          : "Nothing waiting."}
        {queue.next_available_at
          ? ` Next becomes available ${time(queue.next_available_at)}.`
          : ""}
      </Text>
      {queue.last_progress_at && (
        <Text type="supporting">
          Last start or completion: {time(queue.last_progress_at)}
        </Text>
      )}
      {queue.last_terminal_reason && (
        <Text type="supporting">
          Last terminal reason: <Code>{queue.last_terminal_reason}</Code>
          {queue.last_terminal_at ? ` · ${time(queue.last_terminal_at)}` : ""}
        </Text>
      )}
    </VStack>
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
        <Text as="p">
          Showing events for <Code>{workerId}</Code>.{" "}
          <Button
            label={"Show all workers"}
            variant="primary"
            type="submit"
            onClick={clear}
          />
        </Text>
      )}
      <Freshness query={query} quiet />
      {query.data &&
        (query.data.items.length ? (
          <VStack gap={4}>
            {query.data.items.map((event) => (
              <VStack gap={3} key={event.id}>
                <time dateTime={event.occurred_at}>
                  {time(event.occurred_at)}
                </time>
                <Code>{event.event}</Code>
                <Text>
                  <Code>{event.worker_id}</Code>
                </Text>
                <Text>
                  {event.review_run_id !== null && `run ${event.review_run_id}`}
                  {event.review_run_id !== null &&
                    event.job_id !== null &&
                    " · "}
                  {event.job_id !== null && `job ${event.job_id}`}
                </Text>
              </VStack>
            ))}
          </VStack>
        ) : (
          <Empty title="No events recorded" level={3}>
            {workerId
              ? "This worker has recorded no events yet."
              : "Worker starts, stops, and review handoffs will appear here when workers report activity."}
          </Empty>
        ))}
      {query.data && (cursor !== null || query.data.next_cursor !== null) && (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"Newest events"}
            variant="secondary"
            type="submit"

            isDisabled={cursor === null}
            onClick={() => setCursor(null)}
          />
          <Text>Newest first · up to 50 per page</Text>
          <Button
            label={"Older events"}
            variant="secondary"
            type="submit"

            isDisabled={query.data.next_cursor === null}
            onClick={() => setCursor(query.data?.next_cursor ?? null)}
          />
        </HStack>
      )}
    </>
  );
}

export function OperationsPage() {
  const { current } = useScope();
  const [workerId, setWorkerId] = useState("");
  useEffect(() => {
    document.title = "Review Agent · Health";
  }, []);
  const query = useQuery({
    queryKey: ["operations"],
    queryFn: ({ signal }) => read<Operations>("/api/operations", signal),
  });
  const data = query.data;
  const liveCountsKnown =
    data?.queues.every((queue) => Number.isFinite(queue.live_workers)) ?? false;
  const running = liveCountsKnown
    ? (data?.queues.reduce((sum, queue) => sum + queue.live_workers, 0) ?? 0)
    : null;
  const unresponsive =
    data?.workers.filter((worker) => worker.state === "unresponsive").length ??
    0;
  const due = data?.queues.reduce((sum, queue) => sum + queue.due, 0) ?? 0;
  const waitingWithoutWorkers =
    data?.queues.filter(
      (queue) => queue.due > 0 && !(queue.live_workers > 0),
    ) ?? [];
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Health</Heading>
          <Text as="p">
            Check whether reviews can start and where work is waiting.
          </Text>
        </VStack>
      </HStack>
      <Freshness query={query} />
      {data && (
        <>
          <Grid gap={4} columns={{ minWidth: 160, max: 6, repeat: "fit" }}>
            <Stat
              label="Workers online"
              value={running}
              hint="Sending heartbeats"
            />
            <Stat
              label="Unresponsive"
              value={unresponsive}
              hint={
                data.workers_truncated
                  ? "Among shown instances"
                  : "No heartbeat for 90 seconds"
              }
              attention={unresponsive > 0}
            />
            <Stat
              label="Ready to start"
              value={due}
              hint="Queued jobs whose delay has elapsed"
            />
            <Stat
              label="Known worker instances"
              value={data.workers.length}
              hint={
                data.workers_truncated
                  ? "First 100 shown"
                  : "Includes stopped and unresponsive workers"
              }
            />
          </Grid>
          {waitingWithoutWorkers.length > 0 && (
            <VStack gap={3} role="status">
              <strong>Work is waiting without an online worker report</strong>
              <Text as="p">
                {waitingWithoutWorkers
                  .map(
                    (queue) =>
                      `${queueLabels[queue.kind]}: ${number.format(queue.due)} ready`,
                  )
                  .join("; ")}
                . Check the worker services in your deployment platform.
              </Text>
              <Link to="/?status=active">View active requests</Link>
            </VStack>
          )}
          <Section
            title="Workers"
            description="Availability shows worker presence. Assigned jobs show their current work."
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
            description="Follow work from incoming GitHub events to review and publication."
          >
            <Grid
              gap={5}
              maxWidth={900}
              columns={{ minWidth: 240, max: 3, repeat: "fit" }}
            >
              {[...data.queues].sort(byPipeline).map((queue) => (
                <Queue key={queue.kind} queue={queue} />
              ))}
            </Grid>
            {liveCountsKnown ? null : (
              <Text type="supporting">
                Worker counts are unavailable from this API build.
              </Text>
            )}
            <Collapsible
              defaultIsOpen={false}
              trigger={
                <HStack gap={3} wrap="wrap" vAlign="center">
                  How queue states work
                </HStack>
              }
            >
              <VStack gap={4}>
                <Text as="p">
                  Ready jobs have passed their scheduled start time and any
                  recorded quota wait. Team and connection capacity, repository
                  scheduling, and authorization can still delay them. Claimed
                  jobs belong to a worker; an expired claim needs recovery.
                </Text>
                <Text as="p">
                  {running === 0
                    ? "No worker is reporting, so an empty queue does not mean work is being drained."
                    : "A quota wait ending makes a review eligible for another provider check; it does not confirm quota has recovered."}
                </Text>
              </VStack>
            </Collapsible>
          </Section>

          <VStack
            gap={3}
            id="worker-events"

            tabIndex={-1}
            role="region"
            aria-label="Worker events"
          >
            <Section
              title="Events"
              description="Worker starts, stops, and review handoffs. Full logs are available in your deployment platform."
            >
              <Events workerId={workerId} clear={() => setWorkerId("")} />
            </Section>
          </VStack>
        </>
      )}
      <Section
        title="Service connections"
        description="Container status and provider authentication are separate from worker reports."
      >
        <Grid columns={{ minWidth: 300, max: 2, repeat: "fit" }} gap={6}>
          {current.role === "owner" ? <EngineServices /> : null}
          {current.role === "owner" ? <HermesHealth /> : null}
          <GitHubConnection />
          {current.role === "owner" ? <ProviderHealth /> : null}
        </Grid>
      </Section>
    </>
  );
}
