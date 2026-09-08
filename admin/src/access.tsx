import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { components } from "./api.generated";
import { read, write } from "./api";
import { ScopedNavLink as NavLink, isAdmin } from "./scope";
import type { Account } from "./api";
import { Freshness, time } from "./ui";

export function RepositoryTabs({ role }: { role: Account["role"] }) {
  return (
    <nav className="page-tabs" aria-label="Repository views">
      <NavLink to="/repositories">Activity</NavLink>
      {isAdmin(role) && <NavLink to="/access">Access management</NavLink>}
    </nav>
  );
}

type InstallationPage = components["schemas"]["InstallationPage"];
type Installation = components["schemas"]["Installation"];
type RepositoryAccessPage = components["schemas"]["RepositoryAccessPage"];
type RepositoryAccess =
  components["schemas"]["review_agent_tools__admin_access_api__RepositoryAccess"];

const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

type Connection = components["schemas"]["AppConnection"];
type LiveInstallation = components["schemas"]["LiveInstallationStatus"];
const connectionLabels = {
  not_configured: "Not connected",
  connected: "Connected",
  needs_update: "Update required",
  unavailable: "Could not verify",
} as const;

export function GitHubConnection() {
  const query = useQuery({
    queryKey: ["github-connection"],
    queryFn: ({ signal }) => read<Connection>("/api/access/connection", signal),
    refetchInterval: 60_000,
  });
  const data = query.data;
  return (
    <section className="panel github-connection">
      <div className="panel-heading">
        <h2>GitHub App</h2>
        {data && (
          <span
            className={`status ${data.status === "connected" ? "published" : data.status === "needs_update" ? "failed" : "queued"}`}
          >
            {connectionLabels[data.status]}
          </span>
        )}
      </div>
      <div className="panel-body">
        <Freshness query={query} interval={60} />
        {data?.app && (
          <p>
            <strong>{data.app}</strong> · {data.owner}
          </p>
        )}
        {data?.status === "connected" && (
          <p className="field-hint">
            GitHub API authentication and required App permissions verified.
            Installation access and webhook delivery are checked separately.
          </p>
        )}
        {data?.status === "not_configured" && (
          <p>
            Configure the App ID and private key in the admin service’s
            deployment configuration to connect GitHub.
          </p>
        )}
        {!!data?.issues?.length && (
          <ul className="connection-issues">
            {data.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        )}
        <div className="inline-actions">
          <button
            className="secondary"
            type="button"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Check connection now
          </button>
          {data?.edit_url && (
            <a href={data.edit_url} target="_blank" rel="noreferrer">
              Edit GitHub App
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          )}
          {data?.install_url && (
            <a href={data.install_url} target="_blank" rel="noreferrer">
              Install or manage access
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          )}
          {data?.app_url && (
            <a href={data.app_url} target="_blank" rel="noreferrer">
              App page<span className="sr-only"> (opens in a new tab)</span>
            </a>
          )}
        </div>
        {data?.edit_url && (
          <p className="field-hint">
            Editing the App requires access to its owning GitHub account.
            Permission changes may also need approval in each installation.
          </p>
        )}
      </div>
    </section>
  );
}

function InstallationPanel({
  installation,
  configured,
}: {
  installation: Installation;
  configured: boolean;
}) {
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const [policy, setPolicy] = useState<"explicit" | "automatic">(
    installation.repository_activation,
  );
  const [checkEnabled, setCheckEnabled] = useState(false);
  const check = useQuery({
    queryKey: ["access", "installation-status", installation.installation_id],
    queryFn: ({ signal }) =>
      read<LiveInstallation>(
        `/api/access/installations/${installation.installation_id}/status`,
        signal,
      ),
    enabled: checkEnabled && configured,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
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
    <article className="panel installation-panel">
      <div className="panel-heading">
        <div>
          <h2>{installation.account}</h2>
          <span>Installation {installation.installation_id}</span>
        </div>
        <span className={`status ${installation.status}`}>
          {installation.status}
        </span>
      </div>
      <div className="panel-body">
        <dl className="detail-list installation-details">
          <div>
            <dt>GitHub grants access to</dt>
            <dd>
              {installation.repository_selection === "all"
                ? "All repositories"
                : "Selected repositories"}
            </dd>
          </div>
          <div>
            <dt>Review Agent accepts</dt>
            <dd>
              {installation.repository_activation === "automatic"
                ? "All accessible repositories, except manually disabled ones"
                : "Only explicitly enabled repositories"}
            </dd>
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
            <dt>Stored state updated</dt>
            <dd>{dateTime.format(new Date(installation.updated_at))}</dd>
          </div>
        </dl>
        <div className="installation-check">
          <button
            type="button"
            className="secondary"
            disabled={!configured || check.isFetching}
            onClick={() => {
              if (checkEnabled) void check.refetch();
              else setCheckEnabled(true);
            }}
          >
            {check.isFetching ? "Checking GitHub…" : "Check live GitHub status"}
          </button>
          {checkEnabled && <Freshness query={check} quiet />}
          {check.data && (
            <>
              <p>
                GitHub reports <strong>{check.data.status}</strong> ·{" "}
                {check.data.repository_selection === "all"
                  ? "All repositories"
                  : "Selected repositories"}
                . Checked {time(check.data.checked_at)}.
              </p>
              {!!check.data.issues.length && (
                <div className="notice warning">
                  <strong>Installation permissions need an update</strong>
                  <ul>
                    {check.data.issues.map((issue) => (
                      <li key={issue}>{issue}</li>
                    ))}
                  </ul>
                </div>
              )}
              {check.data.settings_url && (
                <a
                  href={check.data.settings_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Edit installation on GitHub
                  <span className="sr-only"> (opens in a new tab)</span>
                </a>
              )}
              {(check.data.status !== installation.status ||
                check.data.repository_selection !==
                  installation.repository_selection) && (
                <p className="field-hint">
                  Stored access differs from GitHub. Save the activation policy
                  below to refresh the installation state.
                </p>
              )}
            </>
          )}
        </div>
        <details className="setup-help">
          <summary>Change Review Agent access</summary>
          <p className="muted">
            Allow all accessible repositories to activate on their first review
            request, or require an administrator to enable each one. Switching
            to explicit enablement disables repositories activated
            automatically; manually enabled repositories remain enabled.
          </p>
          <form
            className="form-fields"
            onSubmit={(event) => {
              event.preventDefault();
              approve.mutate();
            }}
          >
            <label className="field">
              Repositories accepted by Review Agent
              <select
                value={policy}
                disabled={pending || !configured}
                onChange={(event) =>
                  setPolicy(
                    event.target.value === "automatic"
                      ? "automatic"
                      : "explicit",
                  )
                }
              >
                <option value="explicit">
                  Only explicitly enabled repositories
                </option>
                <option value="automatic">All accessible repositories</option>
              </select>
            </label>
            <label className="field grow">
              Reason for change
              <input
                required
                maxLength={500}
                disabled={pending || !configured}
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
              <button disabled={pending || !configured} type="submit">
                {approve.isPending ? "Saving…" : "Save activation policy"}
              </button>
              <button
                disabled={
                  pending ||
                  !reason.trim() ||
                  !configured ||
                  installation.repository_selection !== "selected"
                }
                type="button"
                onClick={() => sync.mutate()}
              >
                {sync.isPending ? "Syncing…" : "Sync repositories"}
              </button>
            </div>
            {installation.repository_selection === "all" && (
              <p className="field-hint">
                All-repository installations discover repositories on the first
                review request. Use Add repository below to enable a specific
                repository now.
              </p>
            )}
            {(approve.isSuccess || sync.isSuccess) && (
              <p role="status">
                {approve.isSuccess
                  ? "Activation policy saved."
                  : "Repository inventory refreshed."}
              </p>
            )}
          </form>
        </details>
      </div>
    </article>
  );
}

function AddRepository({
  configured,
  defaultProfile,
}: {
  configured: boolean;
  defaultProfile: string;
}) {
  const client = useQueryClient();
  const [repository, setRepository] = useState("");
  const [profile, setProfile] = useState(defaultProfile);
  const [reason, setReason] = useState("");
  const add = useMutation({
    mutationFn: () =>
      write<RepositoryAccess>("/api/access/repositories/onboard", "POST", {
        repository: repository.trim(),
        profile: profile.trim(),
        reason: reason.trim(),
      }),
    onSuccess: async () => {
      setRepository("");
      setReason("");
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });
  return (
    <details className="panel add-repository">
      <summary>Add repository</summary>
      <div className="panel-body">
        <p>
          Enable reviews for one repository the GitHub App can access. Works
          with both all-repository and selected-repository installations.
        </p>
        <p className="field-hint">
          For a selected-repository installation, add the repository in GitHub
          first. Disabling a repository here retains its review history.
        </p>
        {!configured && (
          <p className="notice warning">
            Connect the GitHub App credentials before adding repositories.
          </p>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            add.mutate();
          }}
        >
          <fieldset
            disabled={!configured || add.isPending}
            className="settings-fields"
          >
            <label className="field">
              Repository
              <input
                required
                maxLength={260}
                placeholder="owner/repository"
                value={repository}
                onChange={(event) => setRepository(event.target.value)}
              />
            </label>
            <label className="field">
              Review profile
              <input
                required
                maxLength={100}
                value={profile}
                onChange={(event) => setProfile(event.target.value)}
              />
            </label>
            <label className="field">
              Reason for enabling
              <input
                required
                maxLength={500}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          </fieldset>
          {add.error && (
            <p className="notice error" role="alert">
              {add.error.message} Check that the GitHub installation includes
              this repository and has the required permissions.
            </p>
          )}
          {add.isSuccess && (
            <p className="notice" role="status">
              Reviews enabled for {add.data.repository}.
            </p>
          )}
          <button
            disabled={
              !configured ||
              add.isPending ||
              !repository.trim() ||
              !profile.trim() ||
              !reason.trim()
            }
          >
            {add.isPending
              ? "Verifying repository…"
              : "Verify and enable reviews"}
          </button>
        </form>
      </div>
    </details>
  );
}

function RepositoryRow({
  repository,
  defaultProfile,
}: {
  repository: RepositoryAccess;
  defaultProfile: string;
}) {
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const [profile, setProfile] = useState(repository.profile ?? defaultProfile);
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
        <span className="subtext">ID {repository.repository_id}</span>
      </th>
      <td>{repository.access.replaceAll("_", " ")}</td>
      <td>
        {repository.enabled ? "Enabled" : "Disabled"}
        {repository.automatic_activation_blocked && (
          <span className="subtext">Automatic activation blocked</span>
        )}
      </td>
      <td>{repository.profile ?? "—"}</td>
      <td>
        {!repository.enabled && repository.access !== "available" ? (
          <span className="muted">Restore GitHub access before enabling</span>
        ) : (
          <details>
            <summary>
              {repository.enabled ? "Disable reviews…" : "Enable reviews…"}
            </summary>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                mutation.mutate();
              }}
            >
              <p className="muted">
                {repository.enabled
                  ? "Disabling stops new review requests and blocks automatic activation. Review history is retained. To revoke GitHub access too, remove the repository in the GitHub App installation settings."
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
        )}
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
    refetchInterval: false,
    refetchOnWindowFocus: false,
    queryFn: ({ signal }) =>
      read<InstallationPage>(
        `/api/access/installations?limit=50&after_id=${installationCursor}`,
        signal,
      ),
  });
  const repositories = useQuery({
    queryKey: ["access", "repositories", repositoryCursor],
    refetchInterval: false,
    refetchOnWindowFocus: false,
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
      <RepositoryTabs role="admin" />
      <GitHubConnection />
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
        <section className="section">
          <div className="section-heading">
            <h2>Installations</h2>
            <span>{installations.data.items.length} shown</span>
          </div>
          {installations.data.items.length ? (
            <div className="installation-grid">
              {installations.data.items.map((installation) => (
                <InstallationPanel
                  key={installation.installation_id}
                  installation={installation}
                  configured={installations.data.capability.configured}
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
      {capability && (
        <AddRepository
          configured={capability.configured}
          defaultProfile={capability.default_profile}
        />
      )}
      {repositories.data && (
        <section className="section">
          <div className="section-heading">
            <h2>Repositories</h2>
            <span>{repositories.data.items.length} shown</span>
          </div>
          {repositories.data.items.length ? (
            <div className="panel table-scroll">
              <table className="access-table">
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
                      defaultProfile={
                        repositories.data.capability.default_profile
                      }
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
