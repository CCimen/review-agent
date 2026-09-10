import { Button } from "@astryxdesign/core/Button";
import { Code } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
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
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { FormEvent, InputHTMLAttributes } from "react";
import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { APIError, read, write } from "./api";
import type { components } from "./api.generated";
import { ScopedLink as Link, useScope } from "./scope";
import {
  Empty,
  Form,
  Freshness,
  Period,
  Stat,
  number,
  time,
  useFilters,
} from "./ui";

type QualityReport = components["schemas"]["QualityReport"];
type QualityFeedbackPage = components["schemas"]["QualityFeedbackPage"];
type QualityFeedbackItem = components["schemas"]["QualityFeedbackItem"];
type FindingDetail = components["schemas"]["AdminFindingDetail"];
type FindingDecisionRequest =
  components["schemas"]["FindingDecisionRequest"];
type OperatorDecisionResult =
  components["schemas"]["OperatorDecisionResult"];
type QualityTriageRequest = components["schemas"]["QualityTriageRequest"];
type QualityFeedbackTriage = components["schemas"]["QualityFeedbackTriage"];

const age = (seconds: number | null) => {
  if (seconds === null) return "none pending";
  const days = Math.floor(seconds / 86_400);
  return days
    ? `${days}d old`
    : `${Math.max(1, Math.floor(seconds / 3_600))}h old`;
};

function FeedbackRow({
  item,
  admin,
}: {
  item: QualityFeedbackItem;
  admin: boolean;
}) {
  const scope = useScope();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [status, setStatus] =
    useState<QualityTriageRequest["status"]>("actionable");
  const [stableKey, setStableKey] = useState("");
  const [owner, setOwner] =
    useState<NonNullable<QualityTriageRequest["target_owner"]>>(
      "review_rule",
    );
  const mutation = useMutation({
    mutationFn: () => {
      const body: QualityTriageRequest = {
        status,
        reason: `Feedback marked ${status.replaceAll("_", " ")}`,
        stable_key: status === "actionable" ? stableKey : "",
        target_owner: status === "actionable" ? owner : null,
        evidence_reference: "",
        path: "",
        category: "",
      };
      return write<QualityFeedbackTriage>(
        scope.path(`/api/quality/feedback/${item.id}/triage`),
        "POST",
        body,
      );
    },
    onSuccess: async () => {
      setOpen(false);
      await client.invalidateQueries({ queryKey: ["quality"] });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate();
  }
  return (
    <VStack gap={4} as="article">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <strong>
            {item.repository} PR #{item.pr_number}
          </strong>
          <Text color="secondary" display="block" type="supporting">
            Feedback #{item.id} · {time(item.created_at)}
          </Text>
        </VStack>
        <Text>{item.triage_status}</Text>
      </HStack>
      <VStack gap={4}>
        <Text as="p">{item.reason || "No reason was supplied."}</Text>
        {item.stable_key ? (
          <Text as="p" color="secondary" display="block" type="supporting">
            <Code>{item.stable_key}</Code> · {item.target_owner}
          </Text>
        ) : null}
        {item.triage_reason ? (
          <Text as="p" color="secondary" display="block" type="supporting">
            Latest triage: {item.triage_reason}
          </Text>
        ) : null}
        {admin && item.triage_status === "pending" ? (
          <Collapsible
            isOpen={open}
            onOpenChange={(isOpen) => setOpen(isOpen)}
            trigger={
              <HStack gap={3} wrap="wrap" vAlign="center">
                Triage feedback
              </HStack>
            }
          >
            <VStack gap={4}>
              <Form onSubmit={submit}>
                <Selector
                  label={"Outcome"}
                  options={[
                    { value: "actionable", label: "Actionable" },
                    { value: "duplicate", label: "Duplicate" },
                    {
                      value: "insufficient",
                      label: "Insufficient evidence",
                    },
                    { value: "resolved", label: "Resolved" },
                  ]}
                  value={status}
                  onChange={(value) => setStatus(value as typeof status)}
                />
                {status === "actionable" ? (
                  <>
                    <TextInput
                      label={"Stable key"}
                      isRequired={true}
                      value={stableKey}
                      onChange={(value) => setStableKey(value)}
                      {...({
                        required: true,
                        pattern: "[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*",
                        maxLength: 160,
                      } satisfies InputHTMLAttributes<HTMLInputElement>)}
                    />

                    <Selector
                      label={"Owner"}
                      options={[
                        (
                          [
                            "source_tool",
                            "coverage",
                            "review_rule",
                            "profile",
                            "repository_decision",
                            "documentation",
                          ] as const
                        ).map((value) => ({
                          value: String(value),
                          label: value.replaceAll("_", " "),
                        })),
                      ]
                        .flat()
                        .filter((option) => option != null)}
                      value={owner}
                      onChange={(value) => setOwner(value as typeof owner)}
                    />
                  </>
                ) : null}

                {mutation.error ? (
                  <Text as="p">
                    {mutation.error instanceof APIError
                      ? mutation.error.message
                      : "Triage failed."}
                  </Text>
                ) : null}
                <HStack gap={3} wrap="wrap" align="center">
                  <Button
                    label={"Confirm triage"}
                    variant="primary"
                    type="submit"
                    isDisabled={mutation.isPending}
                  />
                </HStack>
              </Form>
            </VStack>
          </Collapsible>
        ) : null}
      </VStack>
    </VStack>
  );
}

export function QualityPage() {
  const scope = useScope();
  const { params, days, update } = useFilters();
  const repository = params.get("repository") ?? "";
  const feedbackOffset = Math.max(
    0,
    Number(params.get("feedback_offset")) || 0,
  );
  const reportParams = new URLSearchParams({ days: String(days) });
  const feedbackParams = new URLSearchParams({
    limit: "50",
    offset: String(feedbackOffset),
  });
  if (repository) {
    reportParams.set("repository", repository);
    feedbackParams.set("repository", repository);
  }
  const report = useQuery({
    queryKey: [
      "quality",
      "report",
      reportParams.toString(),
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<QualityReport>(
        scope.path(`/api/quality?${reportParams}`),
        signal,
      ),
  });
  const feedback = useQuery({
    queryKey: [
      "quality",
      "feedback",
      feedbackParams.toString(),
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<QualityFeedbackPage>(
        scope.path(`/api/quality/feedback?${feedbackParams}`),
        signal,
      ),
  });
  useEffect(() => {
    document.title = "Review Agent · Quality";
  }, []);
  const data = report.data;
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Review quality</Heading>
          <Text as="p">
            Understand reported problems and follow up on review feedback.
          </Text>
        </VStack>
        <Link
          to={`/history?${new URLSearchParams({ days: String(days), status: "published", ...(repository ? { repository } : {}) })}`}
        >
          Browse published reviews
        </Link>
      </HStack>
      <HStack gap={3} wrap="wrap" vAlign="center">
        <Period
          days={days}
          change={(value) => update({ days: value, feedback_offset: "" })}
        />
      </HStack>
      <Freshness query={report} />
      {data ? (
        <>
          <Grid gap={4} columns={{ minWidth: 160, max: 6, repeat: "fit" }}>
            <Stat
              label="Published findings"
              value={data.published_findings}
              hint={`Across ${number.format(data.completed_reviews)} completed reviews`}
            />
            <Stat
              label="Reviews seeing every file"
              value={data.complete_coverage_reviews}
              hint={
                data.completed_reviews
                  ? `of ${number.format(data.completed_reviews)} completed · the rest read part of the change`
                  : "No review completed in this period"
              }
              attention={
                data.completed_reviews > 0 &&
                data.complete_coverage_reviews < data.completed_reviews
              }
            />
            <Stat
              label="Reported false positives"
              value={data.false_positive_signals.count}
              hint={`${number.format(data.false_positive_signals.denominator)} published findings in this period`}
            />
            <Stat
              label="Scope concerns"
              value={data.scope_confusion_signals.count}
              hint={`${number.format(data.scope_confusion_signals.denominator)} completed reviews in this period`}
            />
            <Stat
              label="Reported missed issues"
              value={data.missed_issue_signals.count}
              hint={`${number.format(data.missed_issue_signals.denominator)} completed reviews in this period`}
              attention={data.missed_issue_signals.count > 0}
            />
            <Stat
              label="Awaiting triage"
              value={data.triage_backlog}
              attention={data.triage_backlog > 0}
              hint={
                data.triage_backlog
                  ? `Oldest report: ${age(data.oldest_triage_backlog_seconds)}`
                  : "No reports awaiting a decision"
              }
            />
          </Grid>
          <Text as="p" color="secondary">
            Counts reflect submitted feedback. Reviews without feedback have
            not been assessed here. Pending triage includes all retained
            history.
          </Text>
          <VStack gap={4} as="section">
            <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
              <VStack gap={3}>
                <Heading level={2}>Missed-issue reports</Heading>
                <Text as="p">
                  {feedback.data
                    ? `${feedback.data.pending} pending across ${feedback.data.total} missed-issue reports`
                    : "Loading retained feedback…"}
                </Text>
              </VStack>
            </HStack>
            <Freshness query={feedback} quiet />
            {feedback.data?.items.length ? (
              <>
                <VStack gap={3}>
                  {feedback.data.items.map((item) => (
                    <FeedbackRow
                      key={item.id}
                      item={item}
                      admin={item.can_triage ?? false}
                    />
                  ))}
                </VStack>
                <HStack
                  gap={3}
                  wrap="wrap"
                  vAlign="center"
                  hAlign="between"
                >
                  <Button
                    label={"Previous feedback"}
                    variant="secondary"
                    type="button"
                    isDisabled={feedback.data.offset === 0}
                    onClick={() =>
                      update({
                        feedback_offset: String(
                          Math.max(0, feedback.data!.offset - 50),
                        ),
                      })
                    }
                  />
                  <Text color="secondary" display="block" type="supporting">
                    {number.format(feedback.data.offset + 1)}–
                    {number.format(
                      Math.min(
                        feedback.data.offset + feedback.data.items.length,
                        feedback.data.total,
                      ),
                    )}{" "}
                    of {number.format(feedback.data.total)}
                  </Text>
                  <Button
                    label={"Next feedback"}
                    variant="secondary"
                    type="button"
                    isDisabled={feedback.data.next_offset === null}
                    onClick={() =>
                      update({
                        feedback_offset: String(
                          feedback.data!.next_offset ?? 0,
                        ),
                      })
                    }
                  />
                </HStack>
              </>
            ) : feedback.data ? (
              <Empty
                title={
                  feedback.data.total
                    ? "No reports on this page"
                    : "No missed issues reported"
                }
              >
                {feedback.data.total ? (
                  <Button
                    label={"Return to the first page"}
                    variant="primary"
                    type="submit"
                    onClick={() => update({ feedback_offset: "" })}
                  />
                ) : (
                  "Reports of issues missed by a review will appear here for follow-up."
                )}
              </Empty>
            ) : null}
          </VStack>
          <Collapsible
            defaultIsOpen={false}
            trigger={
              <HStack gap={3} wrap="wrap" vAlign="center">
                Model and policy breakdown
              </HStack>
            }
          >
            <VStack gap={4}>
              <VStack gap={4}>
                <Text as="p" color="secondary">
                  Completed reviews grouped by repository, model, and saved
                  policy. Missing model information is shown as not
                  recorded.
                </Text>
                {data.cohorts_truncated ? (
                  <Text as="p">
                    Showing the first 200 groups. Select a repository or a
                    shorter period to narrow this breakdown.
                  </Text>
                ) : null}
                {data.cohorts.length ? (
                  <VStack
                    gap={0}
                    tabIndex={0}
                    role="region"
                    aria-label="Model and policy breakdown"
                  >
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHeaderCell scope="col">
                            Repository
                          </TableHeaderCell>
                          <TableHeaderCell scope="col">
                            Model
                          </TableHeaderCell>
                          <TableHeaderCell scope="col">
                            Completed reviews
                          </TableHeaderCell>
                          <TableHeaderCell scope="col">
                            Profile
                          </TableHeaderCell>
                          <TableHeaderCell scope="col">
                            Policy
                          </TableHeaderCell>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {data.cohorts.map((cohort) => (
                          <TableRow
                            key={`${cohort.repository}:${cohort.review_contract_hash}:${cohort.policy_revision}`}
                          >
                            <TableHeaderCell scope="row">
                              {cohort.repository}
                            </TableHeaderCell>
                            <TableCell>
                              {cohort.model &&
                              cohort.model !== "unknown" ? (
                                <>
                                  <Code>{cohort.model}</Code>
                                  <Text
                                    color="secondary"
                                    display="block"
                                    type="supporting"
                                  >
                                    {cohort.model_provider !== "unknown"
                                      ? cohort.model_provider
                                      : "Provider not recorded"}
                                  </Text>
                                </>
                              ) : (
                                "Not recorded"
                              )}
                            </TableCell>
                            <TableCell>
                              {number.format(cohort.completed_reviews)}
                            </TableCell>
                            <TableCell>{cohort.profile}</TableCell>
                            <TableCell>{cohort.policy_revision}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </VStack>
                ) : (
                  <Text as="p">No completed reviews in this period.</Text>
                )}
              </VStack>
            </VStack>
          </Collapsible>
        </>
      ) : null}
    </>
  );
}

const decisions: FindingDecisionRequest["decision"][] = [
  "false_positive",
  "intentional_by_design",
  "accepted_risk",
  "duplicate",
  "resolved",
  "reopen",
];

export function FindingPage() {
  const scope = useScope();
  const { fingerprint = "" } = useParams();
  const [params] = useSearchParams();
  const repository = params.get("repository") ?? "";
  const occurrenceId = params.get("occurrence_id") ?? "";
  const decisionsBefore = params.get("decisions_before_id") ?? "";
  const detailParams = new URLSearchParams({
    repository,
    occurrence_id: occurrenceId,
  });
  if (decisionsBefore)
    detailParams.set("decisions_before_id", decisionsBefore);
  const query = useQuery({
    queryKey: [
      "finding",
      repository,
      fingerprint,
      occurrenceId,
      decisionsBefore,
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<FindingDetail>(
        scope.path(
          `/api/findings/${encodeURIComponent(fingerprint)}?${detailParams}`,
        ),
        signal,
      ),
    enabled: Boolean(
      repository && fingerprint && /^[1-9]\d*$/.test(occurrenceId),
    ),
  });
  const client = useQueryClient();
  const [decision, setDecision] =
    useState<FindingDecisionRequest["decision"]>("false_positive");
  const [adr, setAdr] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      write<OperatorDecisionResult>(
        scope.path(
          `/api/findings/${encodeURIComponent(fingerprint)}/decisions`,
        ),
        "POST",
        {
          repository,
          occurrence_id: Number(occurrenceId),
          decision,
          reason: `Finding marked ${decision.replaceAll("_", " ")}`,
          adr_id: decision === "intentional_by_design" ? adr : "",
        } satisfies FindingDecisionRequest,
      ),
    onSuccess: () => {
      setAdr("");
      return client.invalidateQueries({
        queryKey: ["finding", repository, fingerprint],
      });
    },
  });
  useEffect(() => {
    document.title = query.data
      ? `Review Agent · ${query.data.finding.title}`
      : "Review Agent · Finding";
  }, [query.data]);
  if (!repository || !/^[1-9]\d*$/.test(occurrenceId))
    return (
      <Empty title="Exact finding occurrence is required">
        Open this finding from a published review result.
      </Empty>
    );
  const finding = query.data?.finding;
  return (
    <>
      <Freshness query={query} />
      {finding ? (
        <>
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <VStack gap={3}>
              <Text as="p">
                <Link to="/quality">Review quality</Link> ·{" "}
                <Code>{finding.rule_id}</Code>
              </Text>
              <Heading level={1}>{finding.title}</Heading>
              <Text as="p">
                <Code>
                  {finding.path}:{finding.line}
                </Code>{" "}
                · {finding.severity} · {finding.category}
              </Text>
            </VStack>
          </HStack>
          <Grid columns={{ minWidth: 300, max: 2, repeat: "fit" }} gap={6}>
            <VStack gap={4} as="section">
              <VStack gap={4}>
                <Heading level={2}>What the reviewer found</Heading>
                <Text as="p">{finding.evidence}</Text>
                <Heading level={2}>Impact</Heading>
                <Text as="p">{finding.impact}</Text>
                <Heading level={2}>Smallest safe fix</Heading>
                <Text as="p">{finding.smallest_fix}</Text>
              </VStack>
            </VStack>
            <VStack as="aside" gap={3}>
              <VStack gap={4}>
                <Heading level={2}>Human decision</Heading>
                {query.data!.decisions.length ? (
                  query.data!.decisions.map((item) => (
                    <Collapsible
                      key={item.id}
                      defaultIsOpen={false}
                      trigger={
                        <HStack gap={3} wrap="wrap" vAlign="center">
                          {item.decision} · {time(item.created_at)}
                        </HStack>
                      }
                    >
                      <VStack gap={4}>
                        <Text as="p">{item.reason}</Text>
                        <Text
                          as="p"
                          color="secondary"
                          display="block"
                          type="supporting"
                        >
                          {item.actor}
                          {item.adr_id ? ` · ${item.adr_id}` : ""}
                        </Text>
                      </VStack>
                    </Collapsible>
                  ))
                ) : (
                  <Text
                    as="p"
                    color="secondary"
                    display="block"
                    type="supporting"
                  >
                    No decision recorded.
                  </Text>
                )}
                {query.data!.has_more_decisions &&
                query.data!.next_decision_before_id ? (
                  <Link
                    to={`?${new URLSearchParams({ repository, occurrence_id: occurrenceId, decisions_before_id: String(query.data!.next_decision_before_id) })}`}
                  >
                    Older decisions
                  </Link>
                ) : null}
                {query.data?.can_decide ? (
                  <Form
                    onSubmit={(event) => {
                      event.preventDefault();
                      mutation.mutate();
                    }}
                  >
                    <Selector
                      label={"Decision"}
                      options={[
                        decisions.map((value) => ({
                          value: String(value),
                          label: value.replaceAll("_", " "),
                        })),
                      ]
                        .flat()
                        .filter((option) => option != null)}
                      value={decision}
                      onChange={(value) =>
                        setDecision(value as typeof decision)
                      }
                    />
                    {decision === "intentional_by_design" ? (
                      <TextInput
                        label={"Accepted ADR id"}
                        isRequired={true}
                        value={adr}
                        onChange={(value) => setAdr(value)}
                        {...({
                          required: true,
                          maxLength: 80,
                        } satisfies InputHTMLAttributes<HTMLInputElement>)}
                      />
                    ) : null}

                    <Text
                      as="p"
                      color="secondary"
                      display="block"
                      type="supporting"
                    >
                      This records a decision for occurrence #
                      {finding.occurrence_id}. Intentional decisions must
                      match its accepted ADR snapshot and path.
                    </Text>
                    {mutation.error ? (
                      <Text as="p">
                        {mutation.error instanceof APIError
                          ? mutation.error.message
                          : "Decision failed."}
                      </Text>
                    ) : null}
                    <HStack gap={3} wrap="wrap" align="center">
                      <Button
                        label={"Confirm decision"}
                        variant="primary"
                        isDisabled={mutation.isPending}
                        type="submit"
                      />
                    </HStack>
                  </Form>
                ) : null}
              </VStack>
            </VStack>
          </Grid>
        </>
      ) : null}
    </>
  );
}
