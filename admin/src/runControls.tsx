import { useScope } from "./scope";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { APIError, read, write } from "./api";

type RunControlsResponse = components["schemas"]["RunControls"];
type RunAction = "release_retry" | "cancel" | "mark_stalled";

const labels: Record<RunAction, string> = {
  release_retry: "Retry now",
  cancel: "Cancel review",
  mark_stalled: "Mark as stalled",
};

const summaries: Record<RunAction, string> = {
  release_retry:
    "Release this delayed retry now. This keeps the same review request and attempt budget.",
  cancel:
    "Cancel this review and stop further processing of its queued or running job.",
  mark_stalled:
    "Mark this review failed only if its heartbeat is older than the selected cutoff and no live worker lease remains.",
};

export function RunControls({ runId }: { runId: number }) {
  const scope = useScope();
  const queryClient = useQueryClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const [action, setAction] = useState<RunAction | null>(null);
  const [reason, setReason] = useState("");
  const [snapshot, setSnapshot] = useState<RunControlsResponse["job"]>(null);
  const [staleAfterMinutes, setStaleAfterMinutes] = useState(15);
  const query = useQuery({
    queryKey: ["run-controls", runId, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<RunControlsResponse>(
        scope.path(`/api/history/${runId}/controls`),
        signal,
      ),
  });
  const mutation = useMutation({
    mutationFn: async () => {
      const job = snapshot;
      if (!action || !job)
        throw new Error("Refresh the run controls and try again.");
      return write<RunControlsResponse>(
        scope.path(`/api/history/${runId}/actions`),
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
      queryClient.setQueryData(
        ["run-controls", runId, "scoped", scope.key],
        data,
      );
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
        await query.refetch();
      }
    },
  });

  function confirm(nextAction: RunAction) {
    mutation.reset();
    setReason("");
    setSnapshot(query.data?.job ?? null);
    setAction(nextAction);
  }

  const controls = query.data;
  return (
    <>
      <button
        type="button"
        className="secondary"
        aria-haspopup="dialog"
        onClick={() => {
          mutation.reset();
          setAction(null);
          setReason("");
          dialog.current?.showModal();
          void query.refetch();
        }}
      >
        Review actions
      </button>
      <dialog
        ref={dialog}
        className="review-actions-dialog"
        aria-labelledby="run-action-heading"
        onCancel={(event) => {
          if (mutation.isPending) event.preventDefault();
        }}
      >
        {!action ? (
          <>
            <div className="panel-heading">
              <h2 id="run-action-heading">Review actions</h2>
              <button
                type="button"
                className="secondary"
                autoFocus
                onClick={() => dialog.current?.close()}
              >
                Close
              </button>
            </div>
            <p className="muted">Request #{runId}</p>
            {query.isFetching && (
              <p role="status">Checking available actions…</p>
            )}
            {query.isError && (
              <div className="notice error" role="alert">
                <p>Could not check available actions.</p>
                <button type="button" onClick={() => void query.refetch()}>
                  Try again
                </button>
              </div>
            )}
            {mutation.isError && (
              <p className="notice error" role="alert">
                {mutation.error.message}
              </p>
            )}
            {controls && !query.isError && (
              <>
                <div className="review-action-list">
                  {controls.actions.release_retry.available && (
                    <div>
                      <button
                        type="button"
                        disabled={query.isFetching}
                        onClick={() => confirm("release_retry")}
                      >
                        Retry now
                      </button>
                      <p>
                        Start the delayed retry without waiting for its
                        scheduled time.
                      </p>
                    </div>
                  )}
                  {controls.actions.cancel.available && (
                    <div>
                      <button
                        type="button"
                        className="secondary destructive"
                        disabled={query.isFetching}
                        onClick={() => confirm("cancel")}
                      >
                        Cancel review
                      </button>
                      <p>Stop further processing of this request.</p>
                    </div>
                  )}
                </div>
                {controls.actions.mark_stalled.available && (
                  <details className="review-advanced-actions">
                    <summary>Advanced actions</summary>
                    <p>
                      Mark the review as failed if its heartbeat is stale and no
                      worker holds a live lease.
                    </p>
                    <button
                      type="button"
                      className="secondary destructive"
                      disabled={query.isFetching}
                      onClick={() => confirm("mark_stalled")}
                    >
                      Mark as stalled
                    </button>
                  </details>
                )}
                {!Object.values(controls.actions).some(
                  (availability) => availability.available,
                ) && (
                  <p>
                    No actions are available for this request in its current
                    state.
                  </p>
                )}
                {controls.audit.length > 0 && (
                  <details className="review-advanced-actions">
                    <summary>
                      Administrative history ({controls.audit.length})
                    </summary>
                    <ol className="event-list">
                      {controls.audit.map((event) => (
                        <li key={event.id}>
                          <strong>{labels[event.action]}</strong> by{" "}
                          {event.actor}: {event.reason}
                        </li>
                      ))}
                    </ol>
                  </details>
                )}
              </>
            )}
          </>
        ) : (
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
              <button
                className={
                  action === "release_retry"
                    ? undefined
                    : "secondary destructive"
                }
                disabled={mutation.isPending}
                type="submit"
              >
                {mutation.isPending
                  ? "Saving…"
                  : `Confirm ${labels[action].toLowerCase()}`}
              </button>
              <button
                type="button"
                className="secondary"
                disabled={mutation.isPending}
                onClick={() => {
                  setAction(null);
                  mutation.reset();
                }}
              >
                Go back
              </button>
            </div>
          </form>
        )}
      </dialog>
    </>
  );
}
