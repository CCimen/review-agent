import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { APIError, read, write } from "./api";

type RunControlsResponse = components["schemas"]["RunControls"];
type RunAction = "release_retry" | "cancel" | "mark_stalled";

const labels: Record<RunAction, string> = {
  release_retry: "Release retry",
  cancel: "Cancel review",
  mark_stalled: "Mark stalled",
};

const summaries: Record<RunAction, string> = {
  release_retry:
    "Release this delayed retry now. This keeps the same review request and attempt budget.",
  cancel:
    "Cancel this running review. Its queued or leased job becomes terminal immediately.",
  mark_stalled:
    "Mark this review failed only if its heartbeat is older than the selected cutoff and no live worker lease remains.",
};

export function RunControls({ runId }: { runId: number }) {
  const queryClient = useQueryClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const [action, setAction] = useState<RunAction | null>(null);
  const [reason, setReason] = useState("");
  const [snapshot, setSnapshot] = useState<RunControlsResponse["job"]>(null);
  const [staleAfterMinutes, setStaleAfterMinutes] = useState(15);
  const query = useQuery({
    queryKey: ["run-controls", runId],
    queryFn: ({ signal }) =>
      read<RunControlsResponse>(`/api/history/${runId}/controls`, signal),
  });
  const mutation = useMutation({
    mutationFn: async () => {
      const job = snapshot;
      if (!action || !job)
        throw new Error("Refresh the run controls and try again.");
      return write<RunControlsResponse>(
        `/api/history/${runId}/actions`,
        "POST",
        {
          action,
          expected_job_id: job.id,
          expected_lease_generation: job.lease_generation,
          expected_status: job.status,
          expected_available_at: job.available_at,
          reason,
          ...(action === "mark_stalled"
            ? { stale_after_minutes: staleAfterMinutes }
            : {}),
        },
      );
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["run-controls", runId], data);
      setReason("");
      setSnapshot(null);
      setAction(null);
      dialog.current?.close();
      void queryClient.invalidateQueries({
        queryKey: ["review", String(runId)],
      });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
      void queryClient.invalidateQueries({ queryKey: ["activity"] });
    },
    onError: async (error) => {
      if (error instanceof APIError && error.status === 409) {
        setReason("");
        setAction(null);
        dialog.current?.close();
        await query.refetch();
      }
    },
  });

  function confirm(nextAction: RunAction) {
    mutation.reset();
    setReason("");
    setSnapshot(query.data?.job ?? null);
    setAction(nextAction);
    dialog.current?.showModal();
  }

  if (query.isPending) return <p role="status">Loading run controls…</p>;
  if (query.isError)
    return (
      <div className="notice error" role="alert">
        <p>Could not load run controls.</p>
        <button onClick={() => void query.refetch()}>Retry</button>
      </div>
    );
  if (!query.data) return null;

  const controls = query.data;
  const actions: [RunAction, { available: boolean; reason: string }][] = [
    ["release_retry", controls.actions.release_retry],
    ["cancel", controls.actions.cancel],
    ["mark_stalled", controls.actions.mark_stalled],
  ];
  return (
    <section
      className="panel run-controls"
      aria-labelledby="run-controls-heading"
    >
      <div className="panel-heading">
        <div>
          <h2 id="run-controls-heading">Run controls</h2>
          <span>
            {controls.job
              ? `Job ${controls.job.id} · ${controls.job.status.replaceAll("_", " ")}`
              : "No durable job"}
          </span>
        </div>
      </div>
      <div className="button-row">
        {actions.map(([name, availability]) => (
          <button
            key={name}
            type="button"
            disabled={!availability.available}
            title={availability.reason}
            onClick={() => confirm(name)}
          >
            {labels[name]}
          </button>
        ))}
      </div>
      <dl className="detail-list">
        {actions.map(([name, availability]) => (
          <div key={name}>
            <dt>{labels[name]}</dt>
            <dd>{availability.reason}</dd>
          </div>
        ))}
      </dl>
      <dialog ref={dialog} aria-labelledby="run-action-heading">
        {action && (
          <form
            method="dialog"
            onSubmit={(event) => {
              event.preventDefault();
              mutation.mutate();
            }}
          >
            <h2 id="run-action-heading">{labels[action]}</h2>
            <p>{summaries[action]}</p>
            {action === "mark_stalled" && (
              <label className="field">
                Stale after
                <span>
                  <input
                    type="number"
                    required
                    min={1}
                    max={1440}
                    value={staleAfterMinutes}
                    disabled={mutation.isPending}
                    onChange={(event) =>
                      setStaleAfterMinutes(event.currentTarget.valueAsNumber)
                    }
                  />{" "}
                  minutes
                </span>
              </label>
            )}
            <label className="field">
              Reason
              <textarea
                required
                autoFocus
                maxLength={500}
                rows={3}
                value={reason}
                disabled={mutation.isPending}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            {mutation.isError && (
              <p className="notice error" role="alert">
                {mutation.error.message}
              </p>
            )}
            <div className="button-row">
              <button disabled={mutation.isPending} type="submit">
                {mutation.isPending
                  ? "Saving…"
                  : `Confirm ${labels[action].toLowerCase()}`}
              </button>
              <button
                type="button"
                disabled={mutation.isPending}
                onClick={() => dialog.current?.close()}
              >
                Keep run unchanged
              </button>
            </div>
          </form>
        )}
      </dialog>
      {controls.audit.length > 0 && (
        <details>
          <summary>Administrative history ({controls.audit.length})</summary>
          <ol className="event-list">
            {controls.audit.map((event) => (
              <li key={event.id}>
                <strong>{labels[event.action]}</strong> by {event.actor}:{" "}
                {event.reason}
              </li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}
