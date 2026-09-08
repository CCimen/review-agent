import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { read, write } from "./api";
import type { TeamPage } from "./api";
import type { components } from "./api.generated";
import { SettingsTabs } from "./accounts";
import { useScope } from "./scope";
import { ReasonAction } from "./teams";
import { Copy, Empty, Freshness, time } from "./ui";

type Integration = components["schemas"]["Integration"];
type IntegrationPage = components["schemas"]["IntegrationPage"];
type IssuedIntegration = components["schemas"]["IssuedIntegration"];
type IntegrationTeam = components["schemas"]["IntegrationTeam"];

function IntegrationEditor({
  created,
  cancel,
}: {
  created: (issued: IssuedIntegration) => void;
  cancel: () => void;
}) {
  const scope = useScope();
  const [name, setName] = useState("");
  const [deploymentWide, setDeploymentWide] = useState(false);
  const [content, setContent] = useState(false);
  const [selected, setSelected] = useState<IntegrationTeam[]>([]);
  const [search, setSearch] = useState("");
  const [draftSearch, setDraftSearch] = useState("");
  const [expires, setExpires] = useState(() =>
    new Date(Date.now() + 90 * 86_400_000).toISOString().slice(0, 16),
  );
  const [reason, setReason] = useState("");
  const teams = useQuery({
    queryKey: ["integration-team-options", search, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPage>(
        `/api/teams?limit=20&search=${encodeURIComponent(search)}`,
        signal,
      ),
    enabled: !deploymentWide,
  });
  const save = useMutation({
    mutationFn: async () => {
      const issued = await write<IssuedIntegration>(
        "/api/integrations",
        "POST",
        {
          name,
          team_ids: deploymentWide ? [] : selected.map((team) => team.id),
          deployment_wide: deploymentWide,
          read_review_content: content,
          expires_at: new Date(`${expires}Z`).toISOString(),
          reason,
        } satisfies components["schemas"]["IntegrationInput"],
      );
      // Keep the one-time credential out of the shared query/mutation cache.
      created(issued);
    },
  });
  return (
    <form
      className="team-editor"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <h2>New integration</h2>
      <div className="form-fields">
        <label className="field">
          Application name
          <input
            autoFocus
            required
            maxLength={80}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="field">
          Reporting scope
          <select
            value={deploymentWide ? "deployment" : "teams"}
            onChange={(event) =>
              setDeploymentWide(event.target.value === "deployment")
            }
          >
            <option value="teams">Selected teams</option>
            <option value="deployment">Entire deployment</option>
          </select>
        </label>
      </div>
      {deploymentWide ? (
        <p className="notice">
          This application can report on all repositories, including unassigned
          repositories and teams added later.
        </p>
      ) : (
        <fieldset>
          <legend>Approved teams</legend>
          <div className="inline-actions">
            <label className="field grow">
              Find a team
              <input
                type="search"
                maxLength={80}
                value={draftSearch}
                onChange={(event) => setDraftSearch(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="secondary"
              onClick={() => setSearch(draftSearch.trim())}
            >
              Search
            </button>
          </div>
          <Freshness query={teams} quiet />
          {selected.length > 0 && (
            <div className="inline-actions" aria-label="Selected teams">
              {selected.map((team) => (
                <button
                  type="button"
                  className="secondary"
                  key={team.id}
                  onClick={() =>
                    setSelected((items) =>
                      items.filter((item) => item.id !== team.id),
                    )
                  }
                >
                  Remove {team.name}
                </button>
              ))}
            </div>
          )}
          <div className="checkbox-list">
            {teams.data?.items.map((team) => (
              <label key={team.id}>
                <input
                  type="checkbox"
                  checked={selected.some((item) => item.id === team.id)}
                  disabled={
                    selected.length >= 100 &&
                    !selected.some((item) => item.id === team.id)
                  }
                  onChange={(event) =>
                    setSelected((items) =>
                      event.target.checked
                        ? [...items, { id: team.id, name: team.name }]
                        : items.filter((item) => item.id !== team.id),
                    )
                  }
                />
                {team.name}
              </label>
            ))}
          </div>
          {teams.data?.items.length === 0 && <p>No teams match this search.</p>}
          {teams.data?.next_after_id !== null &&
            teams.data?.next_after_id !== undefined && (
              <p>
                Showing the first 20 matches. Narrow the search to find another
                team.
              </p>
            )}
        </fieldset>
      )}
      <label className="check-field">
        <input
          type="checkbox"
          checked={content}
          onChange={(event) => setContent(event.target.checked)}
        />
        Allow published review content
      </label>
      <p className="field-hint">
        All integrations can read outcome metadata and aggregate reports. This
        additional permission exposes published review text within the approved
        scope.
      </p>
      <label className="field">
        Expires at (UTC)
        <input
          type="datetime-local"
          required
          value={expires}
          onChange={(event) => setExpires(event.target.value)}
        />
      </label>
      <label className="field">
        Reason
        <textarea
          required
          maxLength={500}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      {save.isError && (
        <p className="notice error" role="alert">
          {save.error.message}
        </p>
      )}
      <div className="inline-actions">
        <button
          disabled={
            save.isPending ||
            !name.trim() ||
            !reason.trim() ||
            !expires ||
            (!deploymentWide && selected.length === 0)
          }
        >
          {save.isPending ? "Creating…" : "Create integration"}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={save.isPending}
          onClick={cancel}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function IntegrationRow({
  integration,
  refresh,
}: {
  integration: Integration;
  refresh: () => void;
}) {
  return (
    <tr>
      <th scope="row">
        {integration.name}
        <span className="muted"> · #{integration.id}</span>
      </th>
      <td>
        {integration.deployment_wide
          ? "Entire deployment"
          : integration.teams.map((team) => team.name).join(", ")}
      </td>
      <td>
        {integration.read_review_content
          ? "Reports and published content"
          : "Reports only"}
      </td>
      <td>{time(integration.expires_at)}</td>
      <td>
        {integration.state === "active"
          ? "Active"
          : integration.state === "expired"
            ? "Expired"
            : "Revoked"}
      </td>
      <td>
        {integration.state === "active" && (
          <ReasonAction
            label="Revoke"
            path={`/api/integrations/${integration.id}/revoke`}
            description="Subsequent reads will fail. Requests already in progress may finish."
            danger
            done={refresh}
          />
        )}
      </td>
    </tr>
  );
}

export function IntegrationsPage() {
  const scope = useScope();
  const client = useQueryClient();
  const [afterId, setAfterId] = useState(0);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<IssuedIntegration | null>(null);
  const query = useQuery({
    queryKey: ["integrations", afterId, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<IntegrationPage>(
        `/api/integrations?after_id=${afterId}&limit=50`,
        signal,
      ),
  });
  const nextAfterId = query.data?.next_after_id;
  function refresh() {
    void client.invalidateQueries({ queryKey: ["integrations"] });
  }
  useEffect(() => {
    document.title = "Review Agent · Integrations";
  }, []);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Integrations</h1>
          <p>
            Platform administrators grant applications access to approved
            reports.
          </p>
        </div>
        {!creating && !issued && (
          <button onClick={() => setCreating(true)}>New integration</button>
        )}
      </div>
      <SettingsTabs />
      {issued && (
        <section
          className="panel"
          aria-labelledby="integration-credential-title"
        >
          <h2 id="integration-credential-title">
            Save the credential for {issued.integration.name}
          </h2>
          <p>
            This is the only time it is shown. Store it in your application's
            secret manager and send it in the Authorization header as a Bearer
            credential.
          </p>
          <Copy value={issued.token} label="integration credential">
            <code>{issued.token}</code>
          </Copy>
          <p>
            <button className="secondary" onClick={() => setIssued(null)}>
              I have saved it
            </button>
          </p>
        </section>
      )}
      {creating && (
        <IntegrationEditor
          cancel={() => setCreating(false)}
          created={(result) => {
            setIssued(result);
            setCreating(false);
            setAfterId(0);
            refresh();
          }}
        />
      )}
      <Freshness query={query} />
      {query.data &&
        (query.data.items.length ? (
          <div
            className="panel table-scroll"
            tabIndex={0}
            role="region"
            aria-label="Application integrations"
          >
            <table>
              <caption>Credential expiry and granted access</caption>
              <thead>
                <tr>
                  <th scope="col">Application</th>
                  <th scope="col">Scope</th>
                  <th scope="col">Access</th>
                  <th scope="col">Expires</th>
                  <th scope="col">State</th>
                  <th scope="col">Action</th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((integration) => (
                  <IntegrationRow
                    key={integration.id}
                    integration={integration}
                    refresh={refresh}
                  />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="No integrations">
            Create a credential when an application needs to read team reports.
          </Empty>
        ))}
      <div className="pagination">
        {afterId > 0 && (
          <button className="secondary" onClick={() => setAfterId(0)}>
            First page
          </button>
        )}
        {nextAfterId != null && (
          <button className="secondary" onClick={() => setAfterId(nextAfterId)}>
            Next page
          </button>
        )}
      </div>
      <p className="stat-note">
        Permissions are fixed when a credential is created. To change access or
        rotate a credential, create its replacement and then revoke the old
        integration.
      </p>
      <p>
        <a
          href="/api/docs#integration%20reports"
          target="_blank"
          rel="noreferrer"
        >
          Open the API reference
        </a>{" "}
        for request parameters, authentication and response schemas.
      </p>
    </>
  );
}
