import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { read } from "./api";
import type { ActivityCounts, ActivityDay, Overview } from "./api";
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
}: {
  days: { date: string; published_reviews: number }[];
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
  const shown = hovered
    ? days.find((entry) => entry.date === hovered)
    : undefined;
  return (
    <div className="chart-panel panel">
      <div className="chart-head">
        <span className="chart-title">Published reviews per day</span>
        <span className="chart-peak">
          {`${number.format(active)} of ${number.format(days.length)} days`}
        </span>
      </div>
      <div className="chart-figure">
        {/* A y-axis, so a bar height means a number rather than a proportion. */}
        <div className="chart-scale" aria-hidden="true">
          <span>{number.format(peak.published_reviews)}</span>
          <span>0</span>
        </div>
        <ol
          className="chart"
          aria-hidden="true"
          onMouseLeave={() => setHovered(null)}
        >
          {days.map((entry) => (
            <li key={entry.date} onMouseEnter={() => setHovered(entry.date)}>
              <span
                className={
                  entry.published_reviews ? "chart-bar" : "chart-bar zero"
                }
                style={{
                  height: peak.published_reviews
                    ? `${(entry.published_reviews / peak.published_reviews) * 100}%`
                    : "0%",
                }}
              />
            </li>
          ))}
        </ol>
        {/* Values were previously reachable only by mouse-hovering a title
            attribute. This readout is always rendered. */}
        <div className="chart-axis" aria-hidden="true">
          <span>{day(first.date)}</span>
          <span>{day(last.date)}</span>
        </div>
        <p className="chart-readout" aria-hidden="true">
          {shown ? (
            <>
              <strong>{number.format(shown.published_reviews)}</strong>{" "}
              published on {day(shown.date)}
            </>
          ) : (
            <span className="muted">
              {`${number.format(total)} published · busiest ${day(peak.date)} with ${number.format(peak.published_reviews)}`}
            </span>
          )}
        </p>
      </div>
      <p className="sr-only">
        {`${number.format(total)} reviews published across ${days.length} days, from ${day(first.date)} to ${day(last.date)}, on ${number.format(active)} of those days. The busiest day was ${day(peak.date)} with ${number.format(peak.published_reviews)}. Days are grouped at UTC midnight.`}
      </p>
    </div>
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
    <div className="chart-panel panel">
      <div className="chart-head">
        <span className="chart-title">Published reviews per day</span>
        <span className="chart-peak">
          {`${number.format(active.length)} of ${number.format(days.length)} days`}
        </span>
      </div>
      <ol className="day-list">
        {active.map((entry) => (
          <li key={entry.date}>
            <span className="day-date">{day(entry.date)}</span>
            <span className="day-count">
              {number.format(entry.published_reviews)}
            </span>
          </li>
        ))}
      </ol>
      <p className="field-help">
        {`${number.format(total)} published across ${days.length} days in this period. The remaining days had none.`}
      </p>
    </div>
  );
}

function Latency({ counts }: { counts: ActivityCounts }) {
  const median = duration(counts.median_publication_seconds);
  // A 95th percentile over a handful of observations is not a percentile, and
  // this panel does not overclaim anywhere else.
  const enough = counts.published_reviews >= 20;
  const p95 = enough ? duration(counts.p95_publication_seconds) : null;
  return (
    <div className="latency panel">
      <span className="chart-title">Request to publication</span>
      <dl>
        <div>
          <dt>Median</dt>
          <dd>{median ?? <Unknown>No published reviews</Unknown>}</dd>
        </div>
        <div>
          <dt>95th percentile</dt>
          <dd>
            {p95 ?? (
              <Unknown>
                {counts.published_reviews
                  ? "Too few reviews"
                  : "No published reviews"}
              </Unknown>
            )}
          </dd>
        </div>
      </dl>
      <p className="field-help">
        {`Across ${number.format(counts.published_reviews)} published review${counts.published_reviews === 1 ? "" : "s"}, measured from the request to a result reaching GitHub, including queueing, retries and delivery.`}
        {enough ? "" : " A 95th percentile needs at least 20 to mean anything."}
      </p>
    </div>
  );
}

function Failures({
  reasons,
}: {
  reasons: { failure_code: string; requests: number }[];
}) {
  const peak = Math.max(...reasons.map((reason) => reason.requests));
  const comparable = reasons.length > 1;
  return (
    <ol className={comparable ? "bar-list panel" : "bar-list panel plain"}>
      {reasons.map((reason) => (
        <li key={reason.failure_code}>
          <span className="bar-label">
            {failureSentence(reason.failure_code)}
            <code className="subtext">{reason.failure_code}</code>
          </span>
          {comparable ? (
            <span className="bar-track" aria-hidden="true">
              <span
                className="bar-fill"
                style={{ width: `${(reason.requests / peak) * 100}%` }}
              />
            </span>
          ) : (
            <span />
          )}
          <span className="bar-value">
            {number.format(reason.requests)}
            <span className="sr-only">
              {` request${reason.requests === 1 ? "" : "s"}`}
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function Tokens({ counts }: { counts: ActivityCounts }) {
  const caveat = (
    <p className="stat-note">
      Counts come from the pinned model response for each attempt and include
      retries. They do not show monetary cost or remaining subscription quota.
    </p>
  );
  // Nothing recorded needs a sentence, not a grid of four identical blanks.
  if (counts.total_tokens === null)
    return (
      <>
        <p className="stat-note">
          No model usage was recorded in this period. Interrupted or invalid
          responses remain unknown, and earlier reviews are not backfilled.
        </p>
        {caveat}
      </>
    );
  return (
    <>
      <div className="stat-grid">
        <Stat label="Prompt tokens" value={counts.prompt_tokens} />
        <Stat label="Completion tokens" value={counts.completion_tokens} />
        <Stat label="Total tokens" value={counts.total_tokens} />
        <Stat
          label="Attempts reporting usage"
          value={counts.reported_attempts}
        />
      </div>
      {caveat}
    </>
  );
}

function Activity({ counts }: { counts: ActivityCounts }) {
  return (
    <div className="stat-grid">
      <Stat label="Requests started" value={counts.requests} />
      <Stat label="Published reviews" value={counts.published_reviews} />
      <Stat label="PRs reviewed" value={counts.reviewed_prs} />
      <Stat label="Failed requests" value={counts.failed_requests} attention />
    </div>
  );
}

export function OverviewPage() {
  const { days, update } = useFilters();
  useEffect(() => {
    document.title = "Review Agent · Overview";
  }, []);
  const query = useQuery({
    queryKey: ["overview", days],
    queryFn: ({ signal }) =>
      read<Overview>(`/api/overview?days=${days}`, signal),
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
      <div className="page-heading">
        <div>
          <h1>Overview</h1>
          <p>Review activity and delivery for this deployment.</p>
        </div>
      </div>

      {/* Current work and worker presence answer a different question from the
          reporting period, so they are stated separately and first. */}
      <Section
        title="Right now"
        description="Current work and worker presence, independent of the reporting period below."
      >
        <div className="stat-grid live">
          <Stat
            label="Active requests"
            value={data ? data.active_requests : null}
          />
          <Stat
            label="Live review workers"
            value={data ? data.live_review_workers : null}
          />
          <Stat
            label="Review capacity"
            value={data ? data.review_capacity : null}
            hint="Concurrent reviews"
          />
          <Stat
            label="Repositories"
            value={data ? data.repository_count : null}
          />
        </div>
        {data && data.live_review_workers === 0 && data.active_requests > 0 && (
          <p className="notice error">
            {`${number.format(data.active_requests)} request${data.active_requests === 1 ? " is" : "s are"} in progress but no review worker has sent a heartbeat in the last 90 seconds.`}{" "}
            Check <Link to="/operations">Operations</Link> for worker presence
            and queue depth.
          </p>
        )}
      </Section>

      <div className="toolbar">
        <Period days={days} change={(value) => update({ days: value })} />
      </div>
      <Freshness query={query} />

      {data && (
        <>
          <Section
            title={`Last ${days} days`}
            description={`${time(data.window_start)} to ${time(data.window_end)}. Requests are counted from when they started; publications from when the result reached GitHub.`}
          >
            <Activity counts={data.window} />
            <div className="split">
              {series.filter((entry) => entry.published_reviews > 0).length >
              4 ? (
                <Trend days={series} />
              ) : series.length ? (
                <SparseTrend days={series} />
              ) : (
                <div className="chart-panel panel empty-chart">
                  <span className="chart-title">Published reviews per day</span>
                  <p className="muted">
                    No days in this period fall after the earliest retained
                    record.
                  </p>
                </div>
              )}
              <Latency counts={data.window} />
            </div>
          </Section>

          <Section
            title="Why requests failed"
            description="The most common recorded causes in this period. A later request can still have published a review for the same pull request."
          >
            {data.recent_failure_reasons.length ? (
              <Failures reasons={data.recent_failure_reasons} />
            ) : (
              <p className="stat-note">
                No request failed in this period.{" "}
                <Link to={`/history?days=${days}&status=failed`}>
                  Review the failure history
                </Link>{" "}
                to look further back.
              </p>
            )}
          </Section>

          <Section
            title="Token usage"
            description="Model usage recorded for review attempts in this period."
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
            <p className="stat-note">
              {number.format(data.reported_attempts)} of{" "}
              {number.format(data.started_attempts)} lifetime job attempts
              reported token usage. Missing usage remains unknown; some leases
              can finish before calling the model. Recording coverage does not
              verify provider billing.
            </p>
            <p className="stat-note">
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
            </p>
          </Section>
        </>
      )}
    </>
  );
}
