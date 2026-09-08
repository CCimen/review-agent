import { Collapsible } from "@astryxdesign/core/Collapsible";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { read } from "./api";
import type { components } from "./api.generated";
import { Freshness, time } from "./ui";

type Deployment = components["schemas"]["DeploymentStatus"];
type Providers = components["schemas"]["ProviderPage"];
function useDeployment() {
  return useQuery({
    queryKey: ["deployment"],
    queryFn: ({ signal }) => read<Deployment>("/api/deployment", signal),
    refetchInterval: 30000,
  });
}
export function DeploymentLink() {
  const deployment = useDeployment();
  return deployment.data?.dashboard_url ? (
    <AstryxLink
      href={deployment.data.dashboard_url}
      target="_blank"
      rel="noreferrer"
    >
      Open Dokploy<VisuallyHidden> (opens in a new tab)</VisuallyHidden>
    </AstryxLink>
  ) : null;
}
export function EngineServices() {
  const deployment = useDeployment();
  return (
    <VStack gap={4} as="section">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <Heading level={2}>Container status</Heading>
        <Text color="secondary">Dokploy</Text>
      </HStack>
      <VStack gap={4}>
        <Freshness query={deployment} interval={30} />
        {deployment.data?.configured === false ? (
          <VStack gap={4}>
            <strong>Dokploy is not connected</strong>
            <Text as="p">
              Connect Dokploy to see whether this deployment’s containers are
              running.
            </Text>
            <Collapsible
              defaultIsOpen={false}
              trigger={
                <HStack gap={3} wrap="wrap" vAlign="center">
                  How to connect Dokploy
                </HStack>
              }
            >
              <VStack gap={4}>
                <Text as="p">
                  Set the Dokploy URL, Compose application ID, and read-access
                  API key in the admin service’s deployment configuration, then
                  restart that service.
                </Text>
                <AstryxLink
                  href="https://ccimen.github.io/review-agent/admin-panel#dokploy-container-state"
                  target="_blank"
                  rel="noreferrer"
                >
                  Open setup instructions
                  <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
                </AstryxLink>
              </VStack>
            </Collapsible>
          </VStack>
        ) : null}
        {deployment.data?.configured && (
          <>
            <VStack gap={0}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell>Service instance</TableHeaderCell>
                    <TableHeaderCell>State</TableHeaderCell>
                    <TableHeaderCell>Status</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {deployment.data.containers.map((container) => (
                    <TableRow key={container.container_id || container.service}>
                      <TableCell>{container.service}</TableCell>
                      <TableCell>
                        <Text>{container.state}</Text>
                      </TableCell>
                      <TableCell>{container.status}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </VStack>
            {!deployment.data.containers.length && (
              <Text as="p" color="secondary">
                Dokploy returned no containers for this application.
              </Text>
            )}
            <Text as="p" color="secondary">
              {deployment.data.application} · Observed{" "}
              {time(deployment.data.observed_at)}
            </Text>
            <DeploymentLink />
          </>
        )}
      </VStack>
    </VStack>
  );
}
export function ProviderHealth() {
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: ({ signal }) => read<Providers>("/api/providers", signal),
    refetchInterval: 30000,
  });
  return (
    <VStack gap={4} as="section">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <Heading level={2}>Provider authentication</Heading>
        <Link to="/settings">Manage connections</Link>
      </HStack>
      <VStack gap={4}>
        <Freshness query={providers} interval={30} />
        {providers.data?.items.map((provider) => (
          <HStack gap={3} wrap="wrap" vAlign="center" key={provider.provider}>
            <strong>{provider.name}</strong>
            <Text>
              {provider.connected ? "Authenticated" : "Not authenticated"}
            </Text>
          </HStack>
        ))}
        {providers.data?.capability.configured === false && (
          <VStack gap={4}>
            <strong>Provider status is unavailable</strong>
            <Text as="p">
              The connection to the review engine has not been configured.
            </Text>
          </VStack>
        )}
        {providers.data?.capability.configured && (
          <Text as="p" color="secondary">
            Authentication is reported by the review engine. It does not confirm
            that a model request will succeed.
          </Text>
        )}
      </VStack>
    </VStack>
  );
}

type RuntimePage = components["schemas"]["RuntimePage"];
const checkLabels = {
  state_db: "Engine state",
  session_store: "Session storage",
  config: "Configuration",
  model: "Model configuration",
  disk: "Disk space",
  gateway: "Gateway",
  background_queues: "Background queues",
} as const;
export function HermesHealth() {
  const query = useQuery({
    queryKey: ["hermes-runtime"],
    queryFn: ({ signal }) =>
      read<RuntimePage>("/api/providers/runtime", signal),
    refetchInterval: 30_000,
  });
  const runtime = query.data?.runtime;
  return (
    <VStack gap={4} as="section">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <Heading level={2}>Review engine</Heading>
        {runtime && (
          <Text>{runtime.status === "ok" ? "Ready" : "Needs attention"}</Text>
        )}
      </HStack>
      <VStack gap={4}>
        <Freshness query={query} interval={30} />
        {query.data?.capability.configured === false && (
          <VStack gap={4}>
            <strong>Engine diagnostics are not connected</strong>
            <Text as="p">
              Connect the provider companion to read Hermes readiness and its
              running version.
            </Text>
            <Link to="/settings">Open connection setup</Link>
          </VStack>
        )}
        {runtime && (
          <>
            <VStack as="dl" gap={2}>
              <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
                <dt>
                  <Text color="secondary">Hermes version</Text>
                </dt>
                <dd>{runtime.version}</dd>
              </HStack>
              <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
                <dt>
                  <Text color="secondary">Active agents</Text>
                </dt>
                <dd>
                  {runtime.active_agents}
                  {runtime.busy ? " · Busy" : " · Idle"}
                </dd>
              </HStack>
              <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
                <dt>
                  <Text color="secondary">Shutdown state</Text>
                </dt>
                <dd>
                  {runtime.drainable ? "Can drain work" : "Cannot drain work"}
                </dd>
              </HStack>
              <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
                <dt>
                  <Text color="secondary">Default model</Text>
                </dt>
                <dd>{runtime.model ?? "Not reported"}</dd>
              </HStack>
              <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
                <dt>
                  <Text color="secondary">Review API</Text>
                </dt>
                <dd>
                  {runtime.chat_available ? "Supported" : "Not supported"}
                </dd>
              </HStack>
            </VStack>
            <VStack as="ul" gap={3}>
              {runtime.checks.map((check) => (
                <li key={check.name}>
                  <Text>{checkLabels[check.name]}</Text>
                  <Text>{check.status === "ok" ? "OK" : check.status}</Text>
                </li>
              ))}
            </VStack>
            <Text as="p" color="secondary">
              Hermes reports these local checks. They do not send a model
              request. Review Agent can select a different model for each new
              review.
            </Text>
          </>
        )}
      </VStack>
    </VStack>
  );
}
