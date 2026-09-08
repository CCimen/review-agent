import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { read, write } from "./api";

type InstallationPage = components["schemas"]["InstallationPage"];
type Installation = components["schemas"]["Installation"];
type RepositoryAccessPage = components["schemas"]["RepositoryAccessPage"];
type RepositoryAccess =
  components["schemas"]["review_agent_tools__admin_access_api__RepositoryAccess"];

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function InstallationPanel({ installation }: { installation: Installation }) {
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const [policy, setPolicy] = useState<"explicit" | "automatic">(
    installation.repository_activation,
  );
  const approve = useMutation({
    mutationFn: () =>
      write<Installation>(
        `/api/access/installations/${installation.installation_id}/approve`,
        "POST",
        { policy, reason },
      ),
    onSuccess: async () => {
      setReason("");
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });
  const sync = useMutation({
    mutationFn: () =>
      write(
        `/api/access/installations/${installation.installation_id}/sync`,
        "POST",
        { reason },
      ),
    onSuccess: async () => {
      setReason("");
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });
  const pending = approve.isPending || sync.isPending;
  const error = approve.error ?? sync.error;

  return (
    <article className="panel">
      <div className="panel-heading">
        <div>
          <h2>{installation.account}</h2>
          <span>Installation {installation.installation_id}</span>
        </div>
        <span className={`status ${installation.status}`}>
          {installation.status}
        </span>
      </div>
      <dl className="detail-list">
        <div>
          <dt>Repository access</dt>
          <dd>{installation.repository_selection}</dd>
        </div>
        <div>
          <dt>Activation policy</dt>
          <dd>{installation.repository_activation}</dd>
        </div>
        <div>
          <dt>Permissions</dt>
          <dd>
            contents {installation.contents_permission}, issues{" "}
            {installation.issues_permission}, pull requests{" "}
            {installation.pull_requests_permission}
          </dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{dateTime.format(new Date(installation.updated_at))}</dd>
        </div>
      </dl>
      <details>
        <summary>Manage installation</summary>
        <p className="muted">
          Automatic activation admits newly discovered repositories. Explicit
          activation requires an administrator to enable each repository.
        </p>
        <form
          className="form-fields"
          onSubmit={(event) => {
            event.preventDefault();
            approve.mutate();
          }}
        >
          <label className="field">
            Repository activation
            <select
              value={policy}
              disabled={pending}
              onChange={(event) =>
                setPolicy(
                  event.target.value === "automatic" ? "automatic" : "explicit",
                )
              }
            >
              <option value="explicit">Explicit enablement</option>
              <option value="automatic">Automatic activation</option>
            </select>
          </label>
          <label className="field grow">
            Reason
            <input
              required
              maxLength={500}
              disabled={pending}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {error && (
            <p className="notice error" role="alert">
              {error.message}
            </p>
          )}
          <div className="button-row">
            <button disabled={pending} type="submit">
              {approve.isPending ? "Saving…" : "Confirm approval"}
            </button>
            <button
              disabled={pending || !reason.trim()}
              type="button"
              onClick={() => sync.mutate()}
            >
              {sync.isPending ? "Syncing…" : "Sync repositories"}
            </button>
          </div>
        </form>
      </details>
    </article>
  );
}

function RepositoryRow({ repository }: { repository: RepositoryAccess }) {
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const [profile, setProfile] = useState(repository.profile ?? "default");
  const mutation = useMutation({
    mutationFn: () =>
      write<RepositoryAccess>(
        `/api/access/repositories/${repository.repository_id}/${repository.enabled ? "disable" : "enable"}`,
        "POST",
        repository.enabled ? { reason } : { profile, reason },
      ),
    onSuccess: async () => {
      setReason("");
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });

  return (
    <tr>
      <th scope="row">
        <strong>{repository.repository}</strong>
        <span className="muted">ID {repository.repository_id}</span>
      </th>
      <td>{repository.access.replaceAll("_", " ")}</td>
      <td>{repository.enabled ? "Enabled" : "Disabled"}</td>
      <td>{repository.profile ?? "—"}</td>
      <td>
        <details>
          <summary>{repository.enabled ? "Disable…" : "Enable…"}</summary>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              mutation.mutate();
            }}
          >
            <p className="muted">
              {repository.enabled
                ? "Disabling stops new review requests for this repository."
                : "Enabling admits new review requests using the selected profile."}
            </p>
            {!repository.enabled && (
              <label className="field">
                Profile
                <input
                  required
                  maxLength={100}
                  disabled={mutation.isPending}
                  value={profile}
                  onChange={(event) => setProfile(event.target.value)}
                />
              </label>
            )}
            <label className="field">
              Reason
              <input
                required
                maxLength={500}
                disabled={mutation.isPending}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            {mutation.isError && (
              <p className="notice error" role="alert">
                {mutation.error.message}
              </p>
            )}
            <button disabled={mutation.isPending}>
              {mutation.isPending
                ? "Saving…"
                : repository.enabled
                  ? "Confirm disable"
                  : "Confirm enable"}
            </button>
          </form>
        </details>
      </td>
    </tr>
  );
}

export function Access() {
  const [installationCursors, setInstallationCursors] = useState([0]);
  const [repositoryCursors, setRepositoryCursors] = useState([0]);
  const installationCursor = installationCursors.at(-1) ?? 0;
  const repositoryCursor = repositoryCursors.at(-1) ?? 0;
  const installations = useQuery({
    queryKey: ["access", "installations", installationCursor],
    queryFn: ({ signal }) =>
      read<InstallationPage>(
        `/api/access/installations?limit=50&after_id=${installationCursor}`,
        signal,
      ),
  });
  const repositories = useQuery({
    queryKey: ["access", "repositories", repositoryCursor],
    queryFn: ({ signal }) =>
      read<RepositoryAccessPage>(
        `/api/access/repositories?limit=50&after_id=${repositoryCursor}`,
        signal,
      ),
  });
  useEffect(() => {
    document.title = "Review Agent · Repositories & access";
  }, []);

  const capability =
    installations.data?.capability ?? repositories.data?.capability;
  const failed = installations.error ?? repositories.error;
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Repositories &amp; access</h1>
          <p>
            Approve GitHub App installations and control review access for each
            repository.
          </p>
        </div>
      </div>
      {capability && !capability.configured && (
        <p className="notice warning" role="status">
          {capability.detail}
        </p>
      )}
      {(installations.isPending || repositories.isPending) && (
        <p role="status">Loading repository access…</p>
      )}
      {failed && (
        <div className="notice error" role="alert">
          <p>Could not load repository access.</p>
          <button
            onClick={() => {
              void installations.refetch();
              void repositories.refetch();
            }}
          >
            Retry
          </button>
        </div>
      )}
      {installations.data && (
        <section>
          <div className="section-heading">
            <h2>Installations</h2>
            <span>{installations.data.items.length} shown</span>
          </div>
          {installations.data.items.length ? (
            <div className="card-grid">
              {installations.data.items.map((installation) => (
                <InstallationPanel
                  key={installation.installation_id}
                  installation={installation}
                />
              ))}
            </div>
          ) : (
            <p className="empty-state">No GitHub App installations found.</p>
          )}
          <div className="pagination">
            <button
              type="button"
              disabled={installationCursors.length === 1}
              onClick={() =>
                setInstallationCursors((current) => current.slice(0, -1))
              }
            >
              Previous installations
            </button>
            <button
              type="button"
              disabled={installations.data.next_after_id === null}
              onClick={() => {
                const next = installations.data?.next_after_id;
                if (next !== null && next !== undefined)
                  setInstallationCursors((current) => [...current, next]);
              }}
            >
              Next installations
            </button>
          </div>
        </section>
      )}
      {repositories.data && (
        <section>
          <div className="section-heading">
            <h2>Repositories</h2>
            <span>{repositories.data.items.length} shown</span>
          </div>
          {repositories.data.items.length ? (
            <div className="panel table-scroll">
              <table className="repository-table">
                <thead>
                  <tr>
                    <th scope="col">Repository</th>
                    <th scope="col">Provider access</th>
                    <th scope="col">Reviews</th>
                    <th scope="col">Profile</th>
                    <th scope="col">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {repositories.data.items.map((repository) => (
                    <RepositoryRow
                      key={repository.repository_id}
                      repository={repository}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="empty-state">No repositories found.</p>
          )}
          <div className="pagination">
            <button
              type="button"
              disabled={repositoryCursors.length === 1}
              onClick={() =>
                setRepositoryCursors((current) => current.slice(0, -1))
              }
            >
              Previous repositories
            </button>
            <button
              type="button"
              disabled={repositories.data.next_after_id === null}
              onClick={() => {
                const next = repositories.data?.next_after_id;
                if (next !== null && next !== undefined)
                  setRepositoryCursors((current) => [...current, next]);
              }}
            >
              Next repositories
            </button>
          </div>
        </section>
      )}
    </>
  );
}
