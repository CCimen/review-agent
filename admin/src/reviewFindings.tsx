import { ScopedLink as Link, useScope } from "./scope";
import { useQuery } from "@tanstack/react-query";
import { read } from "./api";
import type { components } from "./api.generated";
import { Freshness } from "./ui";

type Findings = components["schemas"]["ReviewFindingPage"];
export function ReviewFindings({
  runId,
  repository,
}: {
  runId: number;
  repository: string;
}) {
  const scope = useScope();
  const query = useQuery({
    queryKey: ["review-findings", runId, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<Findings>(scope.path(`/api/history/${runId}/findings`), signal),
  });
  if (query.data?.total === 0) return null;
  return (
    <details className="finding-index">
      <summary>
        Finding decisions{query.data ? ` · ${query.data.total}` : ""}
      </summary>
      <Freshness query={query} />
      <ul>
        {query.data?.items.map((finding) => (
          <li key={finding.occurrence_id}>
            <Link
              to={`/findings/${encodeURIComponent(finding.fingerprint)}?${new URLSearchParams({ repository, occurrence_id: String(finding.occurrence_id) })}`}
            >
              <span className="mono">
                {finding.local_reference} · {finding.severity}
              </span>{" "}
              {finding.title}
            </Link>
          </li>
        ))}
      </ul>
      {query.data && query.data.total > query.data.items.length && (
        <p className="muted">
          Showing the first {query.data.items.length} published findings.
        </p>
      )}
    </details>
  );
}
