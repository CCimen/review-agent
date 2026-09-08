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
    <a
      className="button secondary"
      href={deployment.data.dashboard_url}
      target="_blank"
      rel="noreferrer"
    >
      Open Dokploy<span className="sr-only"> (opens in a new tab)</span>
    </a>
  ) : null;
}
export function EngineServices() {
  const deployment = useDeployment();
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Container status</h2>
        <span className="muted">Dokploy</span>
      </div>
      <div className="panel-body">
        <Freshness query={deployment} interval={30} />
        {deployment.data?.configured === false ? (
          <div className="integration-empty">
            <strong>Dokploy is not connected</strong>
            <p>
              Connect Dokploy to see whether this deployment’s containers are
              running.
            </p>
            <details className="setup-help">
              <summary>How to connect Dokploy</summary>
              <p>
                Set the Dokploy URL, Compose application ID, and read-access API
                key in the admin service’s deployment configuration, then
                restart that service.
              </p>
              <a
                href="https://ccimen.github.io/review-agent/admin-panel#dokploy-container-state"
                target="_blank"
                rel="noreferrer"
              >
                Open setup instructions
                <span className="sr-only"> (opens in a new tab)</span>
              </a>
            </details>
          </div>
        ) : null}
        {deployment.data?.configured && (
          <>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Service instance</th>
                    <th>State</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {deployment.data.containers.map((container) => (
                    <tr key={container.container_id || container.service}>
                      <td>{container.service}</td>
                      <td>
                        <span
                          className={`status ${container.state === "running" ? "published" : container.state === "exited" ? "" : "queued"}`}
                        >
                          {container.state}
                        </span>
                      </td>
                      <td>{container.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!deployment.data.containers.length && (
              <p className="muted">
                Dokploy returned no containers for this application.
              </p>
            )}
            <p className="muted">
              {deployment.data.application} · Observed{" "}
              {time(deployment.data.observed_at)}
            </p>
            <DeploymentLink />
          </>
        )}
      </div>
    </section>
  );
}
export function ProviderHealth() {
  const providers = useQuery({
    queryKey: ["providers"],
    queryFn: ({ signal }) => read<Providers>("/api/providers", signal),
    refetchInterval: 30000,
  });
  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>Provider authentication</h2>
        <Link to="/settings">Manage connections</Link>
      </div>
      <div className="panel-body">
        <Freshness query={providers} interval={30} />
        {providers.data?.items.map((provider) => (
          <div className="settings-revision" key={provider.provider}>
            <strong>{provider.name}</strong>
            <span>
              {provider.connected ? "Authenticated" : "Not authenticated"}
            </span>
          </div>
        ))}
        {providers.data?.capability.configured === false && (
          <div className="integration-empty">
            <strong>Provider status is unavailable</strong>
            <p>The connection to the review engine has not been configured.</p>
          </div>
        )}
        {providers.data?.capability.configured && (
          <p className="field-hint">
            Authentication is reported by the review engine. It does not confirm
            that a model request will succeed.
          </p>
        )}
      </div>
    </section>
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
    <section className="panel">
      <div className="panel-heading">
        <h2>Review engine</h2>
        {runtime && (
          <span
            className={`status ${runtime.status === "ok" ? "published" : "queued"}`}
          >
            {runtime.status === "ok" ? "Ready" : "Needs attention"}
          </span>
        )}
      </div>
      <div className="panel-body">
        <Freshness query={query} interval={30} />
        {query.data?.capability.configured === false && (
          <div className="integration-empty">
            <strong>Engine diagnostics are not connected</strong>
            <p>
              Connect the provider companion to read Hermes readiness and its
              running version.
            </p>
            <Link to="/settings">Open connection setup</Link>
          </div>
        )}
        {runtime && (
          <>
            <dl className="detail-list">
              <dt>Hermes version</dt>
              <dd>{runtime.version}</dd>
              <dt>Active agents</dt>
              <dd>
                {runtime.active_agents}
                {runtime.busy ? " · Busy" : " · Idle"}
              </dd>
              <dt>Shutdown state</dt>
              <dd>
                {runtime.drainable ? "Can drain work" : "Cannot drain work"}
              </dd>
              <dt>Default model</dt>
              <dd>{runtime.model ?? "Not reported"}</dd>
              <dt>Review API</dt>
              <dd>{runtime.chat_available ? "Supported" : "Not supported"}</dd>
            </dl>
            <ul className="runtime-checks">
              {runtime.checks.map((check) => (
                <li key={check.name}>
                  <span>{checkLabels[check.name]}</span>
                  <span
                    className={check.status === "ok" ? "muted" : "attention"}
                  >
                    {check.status === "ok" ? "OK" : check.status}
                  </span>
                </li>
              ))}
            </ul>
            <p className="field-hint">
              Hermes reports these local checks. They do not send a model
              request. Review Agent can select a different model for each new
              review.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
