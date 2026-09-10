import { Collapsible } from "@astryxdesign/core/Collapsible";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { useQuery } from "@tanstack/react-query";
import { read } from "./api";
import type { components } from "./api.generated";
import { ScopedLink as Link, useScope } from "./scope";
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
    <Collapsible
      defaultIsOpen={false}
      trigger={
        <HStack gap={3} wrap="wrap" vAlign="center">
          Finding decisions{query.data ? ` · ${query.data.total}` : ""}
        </HStack>
      }
    >
      <VStack gap={4}>
        <Freshness query={query} />
        <VStack as="ul" gap={3}>
          {query.data?.items.map((finding) => (
            <li key={finding.occurrence_id}>
              <Link
                to={`/findings/${encodeURIComponent(finding.fingerprint)}?${new URLSearchParams({ repository, occurrence_id: String(finding.occurrence_id) })}`}
              >
                <Text>
                  {finding.local_reference} · {finding.severity}
                </Text>{" "}
                {finding.title}
              </Link>
            </li>
          ))}
        </VStack>
        {query.data && query.data.total > query.data.items.length && (
          <Text as="p" color="secondary">
            Showing the first {query.data.items.length} published findings.
          </Text>
        )}
      </VStack>
    </Collapsible>
  );
}
