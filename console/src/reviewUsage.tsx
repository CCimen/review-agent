import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import type { HistoryItem } from "./api";
import { number } from "./ui";

type ReviewUsage = HistoryItem["usage"];

const tokens = (value: number | null) =>
  value === null ? "Not reported" : number.format(value);

function reportingCoverage(usage: ReviewUsage) {
  const attempts = `${number.format(usage.reported_attempts)} of ${number.format(usage.started_attempts)} attempts reported`;
  return usage.reported_attempts < usage.started_attempts
    ? `Partial · ${attempts}`
    : attempts;
}

/** Per-request usage only. Provider totals include every retry that reported
 * usage; attempt coverage keeps a partial total from reading as complete. */
export function ReviewUsageSummary({
  usage,
  details = false,
}: {
  usage: ReviewUsage;
  details?: boolean;
}) {
  const partial = usage.reported_attempts < usage.started_attempts;
  return (
    <VStack gap={1}>
      <Text hasTabularNumbers>
        {usage.total_tokens === null
          ? "Not reported"
          : `${tokens(usage.total_tokens)} ${details ? "total" : "tokens"}`}
      </Text>
      {details &&
      (usage.prompt_tokens !== null || usage.completion_tokens !== null) ? (
        <Text type="supporting" hasTabularNumbers>
          {tokens(usage.prompt_tokens)} prompt · {tokens(usage.completion_tokens)} completion
        </Text>
      ) : null}
      {details || partial ? (
        <Text type="supporting" color="secondary" hasTabularNumbers>
          {reportingCoverage(usage)}
          {details && usage.started_attempts > 1
            ? " · Reported retries included"
            : ""}
        </Text>
      ) : null}
    </VStack>
  );
}
