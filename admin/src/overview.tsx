import { Banner } from "@astryxdesign/core/Banner";
import { Code } from "@astryxdesign/core/CodeBlock";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import {
  MetadataList,
  MetadataListItem,
} from "@astryxdesign/core/MetadataList";
import { List, ListItem } from "@astryxdesign/core/List";
import { Heading, Text } from "@astryxdesign/core/Text";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ActivityTabs } from "./activity";
import type { ActivityCounts, ActivityDay, Overview } from "./api";
import { read } from "./api";
import { ScopedLink as Link, useScope } from "./scope";
import {
  Freshness,
  Period,
  Section,
  Stat,
  Unknown,
  day,
  duration,
  failureSentence,
  number,
  time,
  useFilters,
} from "./ui";

/** The series omits days with no publications, which would silently compress
 *  the gaps in a chart. Days inside the window are filled with a real zero;
 *  days before the earliest retained record stay out, because no record is not
 *  the same as no publication. */
function dailySeries(
  series: ActivityDay[],
  windowStart: string,
  windowEnd: string,
  retainedSince: string | null,
) {
  const published = new Map(
    series.map((entry) => [entry.date.slice(0, 10), entry.published_reviews]),
  );
  const start = new Date(
    Math.max(
      Date.parse(windowStart),
      retainedSince ? Date.parse(retainedSince) : Number.NEGATIVE_INFINITY,
    ),
  );
  start.setUTCHours(0, 0, 0, 0);
  const end = Date.parse(windowEnd);
  const days: { date: string; published_reviews: number }[] = [];
  for (
    const cursor = start;
    cursor.getTime() <= end && days.length < 400;
    cursor.setUTCDate(cursor.getUTCDate() + 1)
  ) {
    const key = cursor.toISOString().slice(0, 10);
    days.push({ date: key, published_reviews: published.get(key) ?? 0 });
  }
  return days;
}

function Trend({
  days,
  window,
}: {
  days: { date: string; published_reviews: number }[];
  window: number;
}) {
  const [hovered, setHovered] = useState<string | null>(null);
  const first = days[0];
  const last = days[days.length - 1];
  if (!first || !last) return null;
  const peak = days.reduce(
    (best, entry) =>
      entry.published_reviews > best.published_reviews ? entry : best,
    first,
  );
  const total = days.reduce((sum, entry) => sum + entry.published_reviews, 0);
  const active = days.filter((entry) => entry.published_reviews > 0).length;
  const shown = days.find((entry) => entry.date === hovered);
  return (
    <VStack gap={3}>
      <HStack gap={3} wrap="wrap" hAlign="between">
        <Heading level={3}>Published reviews per day</Heading>
        <Text type="supporting">{`${number.format(active)} of ${number.format(days.length)} days`}</Text>
      </HStack>
      <VStack gap={1}>
        <Text type="supporting" aria-hidden="true">
          {number.format(peak.published_reviews)}
        </Text>
        <svg
          viewBox="0 0 600 160"
          width="100%"
          height={160}
          preserveAspectRatio="none"
          aria-hidden="true"
          onMouseLeave={() => setHovered(null)}
        >
          {days.map((entry, index) => {
            const height = peak.published_reviews
              ? (entry.published_reviews / peak.published_reviews) * 160
              : 0;
            return (
              <rect
                key={entry.date}
                x={(index * 600) / days.length}
                y={160 - height}
                width={Math.max(1, 600 / days.length - 2)}
                height={height}
                fill="var(--color-data-categorical-blue)"
                onMouseEnter={() => setHovered(entry.date)}
              />
            );
          })}
          <line
            x1={0}
            y1={160}
            x2={600}
            y2={160}
            stroke="var(--color-border)"
          />
        </svg>
        <HStack hAlign="between" aria-hidden="true">
          <Text type="supporting">{day(first.date)}</Text>
          <Text type="supporting">{day(last.date)}</Text>
        </HStack>
      </VStack>
      <Text>
        {shown
          ? `${number.format(shown.published_reviews)} published on ${day(shown.date)}`
          : `${number.format(total)} published · busiest ${day(peak.date)} with ${number.format(peak.published_reviews)}`}
      </Text>
      {days.length < window ? (
        <Text type="supporting">{`Records begin ${day(first.date)}, so this covers ${days.length} of the last ${window} days.`}</Text>
      ) : null}
      <VisuallyHidden>{`${number.format(total)} reviews published across ${days.length} days, from ${day(first.date)} to ${day(last.date)}, on ${number.format(active)} of those days. The busiest day was ${day(peak.date)} with ${number.format(peak.published_reviews)}. Days are grouped at UTC midnight.`}</VisuallyHidden>
    </VStack>
  );
}

/** Below a handful of publishing days a time series draws thirty empty columns
 *  to carry one number. The days themselves are the more honest presentation. */
function SparseTrend({
  days,
}: {
  days: { date: string; published_reviews: number }[];
}) {
  const active = days.filter((entry) => entry.published_reviews > 0);
  const total = days.reduce((sum, entry) => sum + entry.published_reviews, 0);
  return (
    <VStack gap={4}>
      <HStack gap={3} wrap="wrap" vAlign="center">
        <Heading level={3}>Published reviews per day</Heading>
        <Text type="supporting">
          {`${number.format(active.length)} of ${number.format(days.length)} days`}
        </Text>
      </HStack>
      <List hasDividers>
        {active.map((entry) => (
          <ListItem
            key={entry.date}
            label={day(entry.date)}
            endContent={
              <Text hasTabularNumbers>
                {number.format(entry.published_reviews)}
              </Text>
            }
          />
        ))}
      </List>
      <Text as="p" color="secondary">
        {`${number.format(total)} published across ${days.length} days in this period. The remaining days had none.`}
      </Text>
    </VStack>
  );
}

function Latency({ counts }: { counts: ActivityCounts }) {
  const median = duration(counts.median_publication_seconds);
  // A 95th percentile over a handful of observations is not a percentile, and
  // this panel does not overclaim anywhere else.
  const enough = counts.published_reviews >= 20;
  const p95 = enough ? duration(counts.p95_publication_seconds) : null;
  return (
    <VStack gap={4}>
      <Heading level={3}>Request to publication</Heading>
      <MetadataList label={{ position: "start" }}>
        <MetadataListItem label="Median">
          {median ?? <Unknown>No published reviews</Unknown>}
        </MetadataListItem>
        <MetadataListItem label="95th percentile">
          {p95 ?? (
            <Unknown>
              {counts.published_reviews
                ? "Too few reviews"
                : "No published reviews"}
            </Unknown>
          )}
        </MetadataListItem>
      </MetadataList>
      <Text as="p" color="secondary">
        {`Across ${number.format(counts.published_reviews)} published review${counts.published_reviews === 1 ? "" : "s"}, measured from the request to a result reaching GitHub, including queueing, retries and delivery.`}
        {enough ? "" : " A 95th percentile needs at least 20 to mean anything."}
      </Text>
    </VStack>
  );
}

function Failures({
  reasons,
}: {
  reasons: { failure_code: string; requests: number }[];
}) {
  return (
    <List hasDividers>
      {reasons.map((reason) => (
        <ListItem
          key={reason.failure_code}
          label={failureSentence(reason.failure_code)}
          description={<Code>{reason.failure_code}</Code>}
          endContent={
            <Text hasTabularNumbers>
              {number.format(reason.requests)} requests
            </Text>
          }
        />
      ))}
    </List>
  );
}

function Tokens({ counts }: { counts: ActivityCounts }) {
  const caveat = (
    <Text as="p" color="secondary">
      Counts come from the pinned model response for each attempt and include
      retries. They do not show monetary cost or remaining subscription quota.
    </Text>
  );
  // Nothing recorded needs a sentence, not a grid of four identical blanks.
  if (counts.total_tokens === null)
    return (
      <Text as="p" color="secondary">
        No model usage was recorded in this period. Interrupted or invalid
        responses stay unknown rather than counting as zero, earlier reviews are
        not backfilled, and these counts would describe neither cost nor
        remaining quota.
      </Text>
    );
  return (
    <>
      <Grid gap={4} columns={{ minWidth: 160, max: 6, repeat: "fit" }}>
        <Stat label="Prompt tokens" value={counts.prompt_tokens} />
        <Stat label="Completion tokens" value={counts.completion_tokens} />
        <Stat label="Total tokens" value={counts.total_tokens} />
        <Stat
          label="Attempts reporting usage"
          value={counts.reported_attempts}
        />
      </Grid>
      {caveat}
    </>
  );
}

function Activity({ counts }: { counts: ActivityCounts }) {
  return (
    <Grid gap={4} columns={{ minWidth: 160, max: 6, repeat: "fit" }}>
      <Stat label="Requests started" value={counts.requests} />
      <Stat label="Published reviews" value={counts.published_reviews} />
      <Stat label="PRs reviewed" value={counts.reviewed_prs} />
      <Stat label="Failed requests" value={counts.failed_requests} attention />
    </Grid>
  );
}

export function OverviewPage() {
  const scope = useScope();
  const { days, update } = useFilters();
  useEffect(() => {
    document.title = "Review Agent · Statistics";
  }, []);
  const query = useQuery({
    queryKey: ["overview", days, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<Overview>(scope.path(`/api/overview?days=${days}`), signal),
  });
  const data = query.data;
  const series = data
    ? dailySeries(
        data.daily_publications,
        data.window_start,
        data.window_end,
        data.retained_since,
      )
    : [];
  return (
    <>
      <VStack gap={2}>
        <Heading level={1}>Statistics</Heading>
        <Text as="p" color="secondary">
          Review activity and delivery for this deployment.
        </Text>
      </VStack>

      <ActivityTabs />
      {/* Current work and worker presence answer a different question from the
          reporting period, so they are stated separately and first. */}
      <Section
        title="Right now"
        description="Current work and worker presence, independent of the reporting period below."
      >
        <Grid gap={4} columns={{ minWidth: 160, max: 6, repeat: "fit" }}>
          <Stat
            label="Active requests"
            value={data?.active_requests}
            hint="Queued and running now"
          />
          {data?.live_review_workers != null ? (
            <>
              <Stat
                label="Online review workers"
                value={data.live_review_workers}
                hint="Sent a heartbeat in the last 90 seconds"
              />
              <Stat
                label="Review capacity"
                value={data.review_capacity}
                hint="Concurrent reviews reported"
              />
            </>
          ) : null}
          <Stat
            label="Repositories"
            value={data?.repository_count}
            hint="Known to this deployment"
          />
        </Grid>
        {data && data.live_review_workers === 0 && data.active_requests > 0 && (
          <Banner
            status="warning"
            title="No review worker is reporting"
            collapsible={false}
            description={
              <>
                {`${number.format(data.active_requests)} request${data.active_requests === 1 ? " is" : "s are"} in progress but no review worker has sent a heartbeat in the last 90 seconds.`}{" "}
                Check <Link to="/operations">Operations</Link> for worker
                presence and queue depth.
              </>
            }
          />
        )}
      </Section>

      <HStack gap={3} wrap="wrap" vAlign="center">
        <Period days={days} change={(value) => update({ days: value })} />
      </HStack>
      <Freshness query={query} />

      {data && (
        <>
          <Section
            title={`Last ${days} days`}
            description={`${time(data.window_start)} to ${time(data.window_end)}. Requests are counted from when they started; publications from when the result reached GitHub.`}
          >
            <Activity counts={data.window} />
            <Grid gap={6} columns={{ minWidth: 300, max: 2, repeat: "fit" }}>
              {series.filter((entry) => entry.published_reviews > 0).length >
              4 ? (
                <Trend days={series} window={days} />
              ) : series.length ? (
                <SparseTrend days={series} />
              ) : (
                <VStack gap={4}>
                  <Heading level={3}>Published reviews per day</Heading>
                  <Text as="p" color="secondary">
                    No days in this period fall after the earliest retained
                    record.
                  </Text>
                </VStack>
              )}
              <Latency counts={data.window} />
            </Grid>
          </Section>

          <Section
            title="Why requests failed"
            description="The most common recorded causes in this period. A later request can still have published a review for the same pull request."
          >
            {data.recent_failure_reasons.length ? (
              <Failures reasons={data.recent_failure_reasons} />
            ) : (
              <Text as="p" color="secondary">
                No request failed in this period.{" "}
                <Link to={`/history?days=${days}&status=failed`}>
                  Review the failure history
                </Link>{" "}
                to look further back.
              </Text>
            )}
          </Section>

          <Section
            title="Token usage"
            description={
              data.window.total_tokens === null
                ? undefined
                : "Model usage recorded for review attempts in this period."
            }
          >
            <Tokens counts={data.window} />
          </Section>

          <Section
            title="Lifetime"
            description={
              data.retained_since
                ? `Every retained record since ${time(data.retained_since)}. Activity from before this installation, or from deleted records, is not included.`
                : "No review requests are retained yet."
            }
          >
            <Activity counts={data.lifetime} />
            <Text as="p" color="secondary">
              {number.format(data.reported_attempts)} of{" "}
              {number.format(data.started_attempts)} lifetime job attempts
              reported token usage. Missing usage remains unknown; some leases
              can finish before calling the model. Recording coverage does not
              verify provider billing.
            </Text>
            <Text as="p" color="secondary">
              Median publication{" "}
              {duration(data.lifetime.median_publication_seconds) ?? "—"}
              {" · "}
              95th percentile{" "}
              {duration(data.lifetime.p95_publication_seconds) ?? "—"}
              {" · "}
              Total tokens{" "}
              {data.lifetime.total_tokens === null
                ? "not recorded"
                : number.format(data.lifetime.total_tokens)}
            </Text>
          </Section>
        </>
      )}
    </>
  );
}
