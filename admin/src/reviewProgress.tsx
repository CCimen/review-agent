import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import type { HistoryItem } from "./api";
import { failureSentence, time } from "./ui";

const stateLabels: Record<HistoryItem["state"], string> = {
  queued: "Waiting",
  running: "Reviewing",
  publishing: "Publishing",
  stalled: "Waiting for recovery",
  published: "Published",
  failed: "Failed",
  superseded: "Superseded",
};

const phaseLabels: Record<string, string> = {
  accepted: "Starting review",
  fetching_pr: "Fetching pull request",
  collecting_diff: "Reading changes",
  reviewing: "Reviewing code",
  rendering: "Preparing the result",
  publishing: "Preparing publication",
};

export function reviewStateLabel(item: HistoryItem): string {
  if (item.state === "published" && item.coverage.state !== "complete")
    return "Incomplete";
  if (item.state === "publishing" && item.next_attempt_at)
    return "Waiting to publish";
  return stateLabels[item.state];
}

/** What the state column cannot already say.
 *
 *  Four of the eight states had a progress cell that only re-worded the state
 *  beside it — "Superseded" next to "A newer request or commit replaced this
 *  review" — and those four are the states most rows settle in, so a table of
 *  fifty requests carried fifty sentences that added nothing. A transient
 *  state says what it is waiting on, a failure says its cause, an incomplete
 *  review says how incomplete, and the rest say nothing. */
export function ReviewProgress({ item }: { item: HistoryItem }) {
  let detail: string | null;
  switch (item.state) {
    case "queued":
      detail = item.quota_wait_until
        ? "Waiting for account quota"
        : item.next_attempt_at
          ? "Review attempt scheduled"
          : "Waiting for a worker";
      break;
    case "running":
      detail = phaseLabels[item.phase] ?? "Review in progress";
      break;
    case "publishing":
      detail = item.publication_failure_code
        ? "Publication retry scheduled"
        : "Delivering the result to GitHub";
      break;
    case "stalled":
      detail = "Worker lease expired. Waiting for recovery.";
      break;
    case "failed":
      detail = failureSentence(item.failure_code ?? "");
      break;
    case "superseded":
      detail = null;
      break;
    case "published":
      detail =
        item.coverage.state === "complete"
          ? null
          : item.coverage.changed_files_reported === null
            ? "Coverage was not established"
            : `Complete diffs for ${item.coverage.changed_paths_with_complete_diff} of ${item.coverage.changed_files_reported} changed files`;
      break;
  }
  return (
    <VStack gap={1}>
      {detail ? <Text type="supporting">{detail}</Text> : null}
      {item.quota_wait_until ? (
        <Text type="supporting">
          The next quota check is due {time(item.quota_wait_until)}.
          Work resumes after the provider confirms quota is available.
        </Text>
      ) : null}
      {item.next_attempt_at && (!item.quota_wait_until ||
        Date.parse(item.next_attempt_at) > Date.parse(item.quota_wait_until)) ? (
        <Text type="supporting">Next attempt due: {time(item.next_attempt_at)}</Text>
      ) : null}
    </VStack>
  );
}
