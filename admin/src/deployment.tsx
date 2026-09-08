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
        <h2>Engine services</h2>
        <span className="muted">Container state from Dokploy</span>
      </div>
      <div className="panel-body">
        <Freshness query={deployment} />
        {deployment.data?.configured === false ? (
          <p className="muted">
            Connect this deployment to Dokploy to display container state.
            Worker reports and queue state are available below.
          </p>
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
        <h2>Model providers</h2>
        <Link to="/settings">Manage connections</Link>
      </div>
      <div className="panel-body">
        <Freshness query={providers} />
        {providers.data?.items.map((provider) => (
          <div className="settings-revision" key={provider.provider}>
            <strong>{provider.name}</strong>
            <span>
              {provider.connected ? "Authenticated" : "Not authenticated"}
            </span>
          </div>
        ))}
        {providers.data?.capability.configured === false && (
          <p className="muted">Hermes provider control is not configured.</p>
        )}
        <p className="muted">
          Authentication state comes from Hermes. A completed review provides
          evidence that the model route worked; this page does not call the
          model.
        </p>
      </div>
    </section>
  );
}
