import { ScopedLink as Link, useScope } from "./scope";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { APIError, read, write } from "./api";
import { Empty, Freshness, Period, Stat, number, time, useFilters } from "./ui";

type QualityReport = components["schemas"]["QualityReport"];
type QualityFeedbackPage = components["schemas"]["QualityFeedbackPage"];
type QualityFeedbackItem = components["schemas"]["QualityFeedbackItem"];
type FindingDetail = components["schemas"]["AdminFindingDetail"];
type FindingDecisionRequest = components["schemas"]["FindingDecisionRequest"];
type OperatorDecisionResult = components["schemas"]["OperatorDecisionResult"];
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
  const [reason, setReason] = useState("");
  const [stableKey, setStableKey] = useState("");
  const [owner, setOwner] =
    useState<NonNullable<QualityTriageRequest["target_owner"]>>("review_rule");
  const mutation = useMutation({
    mutationFn: () => {
      const body: QualityTriageRequest = {
        status,
        reason,
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
      setReason("");
      await client.invalidateQueries({ queryKey: ["quality"] });
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate();
  }
  return (
    <article className="panel quality-feedback">
      <div className="panel-heading">
        <div>
          <strong>
            {item.repository} PR #{item.pr_number}
          </strong>
          <span className="subtext">
            Feedback #{item.id} · {time(item.created_at)}
          </span>
        </div>
        <span
          className={`status ${item.triage_status === "pending" ? "queued" : ""}`}
        >
          {item.triage_status}
        </span>
      </div>
      <div className="panel-body">
        <p>{item.reason || "No reason was supplied."}</p>
        {item.stable_key ? (
          <p className="subtext">
            <code>{item.stable_key}</code> · {item.target_owner}
          </p>
        ) : null}
        {item.triage_reason ? (
          <p className="subtext">Latest triage: {item.triage_reason}</p>
        ) : null}
        {admin && item.triage_status === "pending" ? (
          <details
            open={open}
            onToggle={(event) => setOpen(event.currentTarget.open)}
          >
            <summary>Triage feedback</summary>
            <form className="decision-form" onSubmit={submit}>
              <label className="field">
                Outcome
                <select
                  value={status}
                  onChange={(event) =>
                    setStatus(event.target.value as typeof status)
                  }
                >
                  <option value="actionable">Actionable</option>
                  <option value="duplicate">Duplicate</option>
                  <option value="insufficient">Insufficient evidence</option>
                  <option value="resolved">Resolved</option>
                </select>
              </label>
              {status === "actionable" ? (
                <>
                  <label className="field">
                    Stable key
                    <input
                      required
                      pattern="[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*"
                      maxLength={160}
                      value={stableKey}
                      onChange={(event) => setStableKey(event.target.value)}
                    />
                  </label>
                  <label className="field">
                    Owner
                    <select
                      value={owner}
                      onChange={(event) =>
                        setOwner(event.target.value as typeof owner)
                      }
                    >
                      {(
                        [
                          "source_tool",
                          "coverage",
                          "review_rule",
                          "profile",
                          "repository_decision",
                          "documentation",
                        ] as const
                      ).map((value) => (
                        <option key={value} value={value}>
                          {value.replaceAll("_", " ")}
                        </option>
                      ))}
                    </select>
                  </label>
                </>
              ) : null}
              <label className="field">
                Reason
                <textarea
                  required
                  maxLength={2000}
                  rows={3}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>
              {mutation.error ? (
                <p className="form-error">
                  {mutation.error instanceof APIError
                    ? mutation.error.message
                    : "Triage failed."}
                </p>
              ) : null}
              <button type="submit" disabled={mutation.isPending}>
                Confirm triage
              </button>
            </form>
          </details>
        ) : null}
      </div>
    </article>
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
      read<QualityReport>(scope.path(`/api/quality?${reportParams}`), signal),
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
      <div className="page-heading">
        <div>
          <h1>Review quality</h1>
          <p>Understand reported problems and follow up on review feedback.</p>
        </div>
        <Link
          className="button secondary"
          to={`/history?${new URLSearchParams({ days: String(days), status: "published", ...(repository ? { repository } : {}) })}`}
        >
          Browse published reviews
        </Link>
      </div>
      <div className="toolbar">
        <Period
          days={days}
          change={(value) => update({ days: value, feedback_offset: "" })}
        />
      </div>
      <Freshness query={report} />
      {data ? (
        <>
          <div className="stat-grid">
            <Stat label="Published findings" value={data.published_findings} />
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
          </div>
          <p className="stat-note">
            Counts reflect submitted feedback. Reviews without feedback have not
            been assessed here. Pending triage includes all retained history.
          </p>
          <section className="section">
            <div className="section-heading">
              <div>
                <h2>Missed-issue reports</h2>
                <p>
                  {feedback.data
                    ? `${feedback.data.pending} pending across ${feedback.data.total} missed-issue reports`
                    : "Loading retained feedback…"}
                </p>
              </div>
            </div>
            <Freshness query={feedback} quiet />
            {feedback.data?.items.length ? (
              <>
                <div className="quality-list">
                  {feedback.data.items.map((item) => (
                    <FeedbackRow
                      key={item.id}
                      item={item}
                      admin={item.can_triage ?? false}
                    />
                  ))}
                </div>
                <div className="pagination">
                  <button
                    className="secondary"
                    type="button"
                    disabled={feedback.data.offset === 0}
                    onClick={() =>
                      update({
                        feedback_offset: String(
                          Math.max(0, feedback.data!.offset - 50),
                        ),
                      })
                    }
                  >
                    Previous feedback
                  </button>
                  <span className="subtext">
                    {number.format(feedback.data.offset + 1)}–
                    {number.format(
                      Math.min(
                        feedback.data.offset + feedback.data.items.length,
                        feedback.data.total,
                      ),
                    )}{" "}
                    of {number.format(feedback.data.total)}
                  </span>
                  <button
                    className="secondary"
                    type="button"
                    disabled={feedback.data.next_offset === null}
                    onClick={() =>
                      update({
                        feedback_offset: String(
                          feedback.data!.next_offset ?? 0,
                        ),
                      })
                    }
                  >
                    Next feedback
                  </button>
                </div>
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
                  <button
                    className="text-button"
                    onClick={() => update({ feedback_offset: "" })}
                  >
                    Return to the first page
                  </button>
                ) : (
                  "Reports of issues missed by a review will appear here for follow-up."
                )}
              </Empty>
            ) : null}
          </section>
          <details className="panel report-breakdown">
            <summary>Model and policy breakdown</summary>
            <div className="panel-body">
              <p className="field-hint">
                Completed reviews grouped by repository, model, and saved
                policy. Missing model information is shown as not recorded.
              </p>
              {data.cohorts_truncated ? (
                <p className="notice">
                  Showing the first 200 groups. Select a repository or a shorter
                  period to narrow this breakdown.
                </p>
              ) : null}
              {data.cohorts.length ? (
                <div
                  className="table-scroll"
                  tabIndex={0}
                  role="region"
                  aria-label="Model and policy breakdown"
                >
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">Repository</th>
                        <th scope="col">Model</th>
                        <th scope="col">Completed reviews</th>
                        <th scope="col">Profile</th>
                        <th scope="col">Policy</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.cohorts.map((cohort) => (
                        <tr
                          key={`${cohort.repository}:${cohort.review_contract_hash}:${cohort.policy_revision}`}
                        >
                          <th scope="row">{cohort.repository}</th>
                          <td>
                            {cohort.model && cohort.model !== "unknown" ? (
                              <>
                                <code>{cohort.model}</code>
                                <span className="subtext">
                                  {cohort.model_provider !== "unknown"
                                    ? cohort.model_provider
                                    : "Provider not recorded"}
                                </span>
                              </>
                            ) : (
                              "Not recorded"
                            )}
                          </td>
                          <td className="numeric">
                            {number.format(cohort.completed_reviews)}
                          </td>
                          <td>{cohort.profile}</td>
                          <td>{cohort.policy_revision}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p>No completed reviews in this period.</p>
              )}
            </div>
          </details>
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
  if (decisionsBefore) detailParams.set("decisions_before_id", decisionsBefore);
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
  const [reason, setReason] = useState("");
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
          reason,
          adr_id: decision === "intentional_by_design" ? adr : "",
        } satisfies FindingDecisionRequest,
      ),
    onSuccess: () => {
      setReason("");
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
          <div className="page-heading">
            <div>
              <p>
                <Link to="/quality">Review quality</Link> ·{" "}
                <code>{finding.rule_id}</code>
              </p>
              <h1>{finding.title}</h1>
              <p>
                <code>
                  {finding.path}:{finding.line}
                </code>{" "}
                · {finding.severity} · {finding.category}
              </p>
            </div>
          </div>
          <div className="quality-detail">
            <section className="panel">
              <div className="panel-body">
                <h2>What the reviewer found</h2>
                <p>{finding.evidence}</p>
                <h2>Impact</h2>
                <p>{finding.impact}</p>
                <h2>Smallest safe fix</h2>
                <p>{finding.smallest_fix}</p>
              </div>
            </section>
            <aside className="panel">
              <div className="panel-body">
                <h2>Human decision</h2>
                {query.data!.decisions.length ? (
                  query.data!.decisions.map((item) => (
                    <details key={item.id}>
                      <summary>
                        {item.decision} · {time(item.created_at)}
                      </summary>
                      <p>{item.reason}</p>
                      <p className="subtext">
                        {item.actor}
                        {item.adr_id ? ` · ${item.adr_id}` : ""}
                      </p>
                    </details>
                  ))
                ) : (
                  <p className="subtext">No decision recorded.</p>
                )}
                {query.data!.has_more_decisions &&
                query.data!.next_decision_before_id ? (
                  <Link
                    className="button secondary"
                    to={`?${new URLSearchParams({ repository, occurrence_id: occurrenceId, decisions_before_id: String(query.data!.next_decision_before_id) })}`}
                  >
                    Older decisions
                  </Link>
                ) : null}
                {query.data?.can_decide ? (
                  <form
                    className="decision-form"
                    onSubmit={(event) => {
                      event.preventDefault();
                      mutation.mutate();
                    }}
                  >
                    <label className="field">
                      Decision
                      <select
                        value={decision}
                        onChange={(event) =>
                          setDecision(event.target.value as typeof decision)
                        }
                      >
                        {decisions.map((value) => (
                          <option key={value} value={value}>
                            {value.replaceAll("_", " ")}
                          </option>
                        ))}
                      </select>
                    </label>
                    {decision === "intentional_by_design" ? (
                      <label className="field">
                        Accepted ADR id
                        <input
                          required
                          maxLength={80}
                          value={adr}
                          onChange={(event) => setAdr(event.target.value)}
                        />
                      </label>
                    ) : null}
                    <label className="field">
                      Reason
                      <textarea
                        required
                        maxLength={2000}
                        rows={4}
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                      />
                    </label>
                    <p className="subtext">
                      This records a decision for occurrence #
                      {finding.occurrence_id}. Intentional decisions must match
                      its accepted ADR snapshot and path.
                    </p>
                    {mutation.error ? (
                      <p className="form-error">
                        {mutation.error instanceof APIError
                          ? mutation.error.message
                          : "Decision failed."}
                      </p>
                    ) : null}
                    <button disabled={mutation.isPending} type="submit">
                      Confirm decision
                    </button>
                  </form>
                ) : null}
              </div>
            </aside>
          </div>
        </>
      ) : null}
    </>
  );
}
