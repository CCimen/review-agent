import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Dialog } from "@astryxdesign/core/Dialog";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Section } from "@astryxdesign/core/Section";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { useState } from "react";
import { APIError, read, write } from "./api";
import type { components } from "./api.generated";
import { useScope } from "./scope";
import { Form } from "./ui";

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
  const [isOpen, setIsOpen] = useState(false);
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
      setIsOpen(false);
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
      <Button
        label={"Review actions"}
        variant="secondary"
        type="button"

        aria-haspopup="dialog"
        onClick={() => {
          mutation.reset();
          setAction(null);
          setReason("");
          setIsOpen(true);
          void query.refetch();
        }}
      />
      <Dialog
        isOpen={isOpen}
        width={560}
        onOpenChange={(open) => {
          if (!mutation.isPending) setIsOpen(open);
        }}
        aria-labelledby="run-action-heading"
      >
        <Section padding={6}>
          <VStack gap={4}>
            {!action ? (
              <>
                <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
                  <Heading level={2} id="run-action-heading">
                    Review actions
                  </Heading>
                  <Button
                    label={"Close"}
                    variant="secondary"
                    type="button"

                    onClick={() => setIsOpen(false)}
                  />
                </HStack>
                <Text as="p" color="secondary">
                  Request #{runId}
                </Text>
                {query.isFetching && (
                  <Text as="p" role="status">
                    Checking available actions…
                  </Text>
                )}
                {query.isError && (
                  <VStack gap={3} role="alert">
                    <Text as="p">Could not check available actions.</Text>
                    <Button
                      label={"Try again"}
                      variant="primary"
                      type="button"
                      onClick={() => void query.refetch()}
                    />
                  </VStack>
                )}
                {mutation.isError && (
                  <Text as="p" role="alert">
                    {mutation.error.message}
                  </Text>
                )}
                {controls && !query.isError && (
                  <>
                    <HStack gap={3} wrap="wrap" vAlign="center">
                      {controls.actions.release_retry.available && (
                        <VStack gap={3}>
                          <Button
                            label={"Retry now"}
                            variant="primary"
                            type="button"
                            isDisabled={query.isFetching}
                            onClick={() => confirm("release_retry")}
                          />
                          <Text as="p">
                            Start the delayed retry without waiting for its
                            scheduled time.
                          </Text>
                        </VStack>
                      )}
                      {controls.actions.cancel.available && (
                        <VStack gap={3}>
                          <Button
                            label={"Cancel review"}
                            variant="destructive"
                            type="button"

                            isDisabled={query.isFetching}
                            onClick={() => confirm("cancel")}
                          />
                          <Text as="p">
                            Stop further processing of this request.
                          </Text>
                        </VStack>
                      )}
                    </HStack>
                    {controls.actions.mark_stalled.available && (
                      <Collapsible
                        defaultIsOpen={false}
                        trigger={
                          <HStack gap={3} wrap="wrap" vAlign="center">
                            Advanced actions
                          </HStack>
                        }
                      >
                        <VStack gap={4}>
                          <Text as="p">
                            Mark the review as failed if its heartbeat is stale
                            and no worker holds a live lease.
                          </Text>
                          <Button
                            label={"Mark as stalled"}
                            variant="destructive"
                            type="button"

                            isDisabled={query.isFetching}
                            onClick={() => confirm("mark_stalled")}
                          />
                        </VStack>
                      </Collapsible>
                    )}
                    {!Object.values(controls.actions).some(
                      (availability) => availability.available,
                    ) && (
                      <Text as="p">
                        No actions are available for this request in its current
                        state.
                      </Text>
                    )}
                    {controls.audit.length > 0 && (
                      <Collapsible
                        defaultIsOpen={false}
                        trigger={
                          <HStack gap={3} wrap="wrap" vAlign="center">
                            Administrative history ({controls.audit.length})
                          </HStack>
                        }
                      >
                        <VStack gap={4}>
                          <VStack as="ol" gap={3}>
                            {controls.audit.map((event) => (
                              <li key={event.id}>
                                <strong>{labels[event.action]}</strong> by{" "}
                                {event.actor}: {event.reason}
                              </li>
                            ))}
                          </VStack>
                        </VStack>
                      </Collapsible>
                    )}
                  </>
                )}
              </>
            ) : (
              <Form
                method="dialog"
                onSubmit={(event) => {
                  event.preventDefault();
                  mutation.mutate();
                }}
              >
                <Heading level={2} id="run-action-heading">
                  {labels[action]}
                </Heading>
                <Text as="p">{summaries[action]}</Text>
                {action === "mark_stalled" && (
                  <NumberInput
                    label="Stale after (minutes)"
                    isRequired
                    min={1}
                    max={1440}
                    isIntegerOnly
                    value={staleAfterMinutes}
                    isDisabled={mutation.isPending}
                    onChange={setStaleAfterMinutes}
                    {...({
                      required: true,
                    } satisfies InputHTMLAttributes<HTMLInputElement>)}
                  />
                )}

                <TextArea
                  label={"Reason"}
                  isRequired={true}
                  hasAutoFocus={true}
                  maxLength={500}
                  rows={3}
                  value={reason}
                  isDisabled={mutation.isPending}
                  onChange={(value) => setReason(value.slice(0, 500))}
                  {...({
                    required: true,
                  } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
                />

                {mutation.isError && (
                  <Text as="p" role="alert">
                    {mutation.error.message}
                  </Text>
                )}
                <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
                  <Button
                    label={
                      mutation.isPending
                        ? "Saving…"
                        : `Confirm ${labels[action].toLowerCase()}`
                    }
                    variant="primary"

                    isDisabled={mutation.isPending}
                    type="submit"
                  />
                  <Button
                    label={"Go back"}
                    variant="secondary"
                    type="button"

                    isDisabled={mutation.isPending}
                    onClick={() => {
                      setAction(null);
                      mutation.reset();
                    }}
                  />
                </HStack>
              </Form>
            )}
          </VStack>
        </Section>
      </Dialog>
    </>
  );
}
