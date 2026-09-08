import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { read, write } from "./api";
import type {
  Team,
  TeamPage,
  TeamMemberPage,
  RepositoryRequestPage,
  TeamRepositoryPage,
} from "./api";
import type { components } from "./api.generated";
import { ScopedLink as Link, useScope, isAdmin } from "./scope";
import { Empty, Freshness, time } from "./ui";
import { AuditLog } from "./audit";

type TeamRole = components["schemas"]["TeamRole"];
type ModelPolicy = components["schemas"]["TeamModelPolicy"];
type ModelConnection = components["schemas"]["ModelConnection"];
type ModelConnectionPage = components["schemas"]["ConnectionPage"];

function useTeamRefresh() {
  const client = useQueryClient();
  return () =>
    Promise.all(
      [
        "teams",
        "team",
        "team-members",
        "team-repositories",
        "repository-requests",
        "audit",
        "me",
        "repositories",
        "overview",
      ].map((name) => client.invalidateQueries({ queryKey: [name] })),
    );
}

export function ReasonAction({
  label,
  path,
  body,
  description,
  done,
  method = "POST",
  danger = false,
}: {
  label: string;
  path: string;
  body?: Record<string, unknown>;
  description: string;
  done?: () => void;
  method?: "POST" | "PUT";
  danger?: boolean;
}) {
  const refresh = useTeamRefresh();
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState(false);
  const action = useMutation({
    mutationFn: () => write(path, method, { ...body, reason }),
    onSuccess: async () => {
      setReason("");
      setOpen(false);
      await refresh();
      done?.();
    },
  });
  return (
    <div className="reason-action">
      <button
        type="button"
        className={danger ? "text-button danger" : "text-button"}
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
          action.reset();
        }}
      >
        {label}
      </button>
      {open ? (
        <form
          className="reason-form"
          onSubmit={(event) => {
            event.preventDefault();
            action.mutate();
          }}
        >
          <p>{description}</p>
          <label className="field">
            Reason
            <textarea
              autoFocus
              required
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {action.isError ? (
            <p className="notice error" role="alert">
              {action.error.message}
            </p>
          ) : null}
          <div className="inline-actions">
            <button disabled={action.isPending || !reason.trim()}>
              {action.isPending ? "Saving…" : label}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={action.isPending}
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

function TeamEditor({ team, done }: { team?: Team; done?: () => void }) {
  const refresh = useTeamRefresh();
  const [name, setName] = useState(team?.name ?? "");
  const [description, setDescription] = useState(team?.description ?? "");
  const [reason, setReason] = useState("");
  const save = useMutation({
    mutationFn: () =>
      team
        ? write<Team>(`/api/teams/${team.id}`, "PATCH", {
            name,
            description,
            reason,
            expected_revision: team.revision,
          } satisfies components["schemas"]["TeamUpdate"])
        : write<Team>("/api/teams", "POST", {
            name,
            description,
            reason,
          } satisfies components["schemas"]["NewTeam"]),
    onSuccess: async () => {
      await refresh();
      setReason("");
      done?.();
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
      <div className="form-fields">
        <label className="field">
          Team name
          <input
            autoFocus
            required
            maxLength={80}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <label className="field grow">
          Description
          <input
            maxLength={500}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
      </div>
      <label className="field">
        Reason
        <textarea
          required
          maxLength={500}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      {save.isError ? (
        <p className="notice error" role="alert">
          {save.error.message}
        </p>
      ) : null}
      {save.isSuccess ? <p role="status">Team saved.</p> : null}
      <button disabled={save.isPending}>
        {save.isPending ? "Saving…" : team ? "Save team" : "Create team"}
      </button>
    </form>
  );
}

export function TeamsPage() {
  const scope = useScope();
  const [params, setParams] = useSearchParams();
  const search = params.get("search") ?? "";
  const [draft, setDraft] = useState(search);
  const [adding, setAdding] = useState(false);
  const after = params.get("after_id") ?? "0";
  const requestedRepository = params.get("assign_repository") ?? "";
  const assignRepository =
    isAdmin(scope.current.role) && /^[1-9]\d{0,18}$/.test(requestedRepository)
      ? requestedRepository
      : null;
  const repositoryName =
    params.get("repository_name") ?? `Repository #${assignRepository}`;
  function clearAssignment() {
    const next = new URLSearchParams(params);
    next.delete("assign_repository");
    next.delete("repository_name");
    setParams(next);
  }
  const query = useQuery({
    queryKey: ["teams", "list", search, after, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPage>(
        `/api/teams?${new URLSearchParams({ search, after_id: after })}`,
        signal,
      ),
  });
  useEffect(() => {
    document.title = "Review Agent · Teams";
  }, []);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Teams</h1>
          <p>
            {isAdmin(scope.current.role)
              ? "Platform administration · Manage team membership and repository ownership."
              : "Your teams, repositories, and access."}
          </p>
        </div>
        {isAdmin(scope.current.role) ? (
          <button onClick={() => setAdding(!adding)} aria-expanded={adding}>
            {adding ? "Close form" : "Create team"}
          </button>
        ) : null}
      </div>
      <nav className="page-tabs" aria-label="Team administration">
        <Link to="/teams">Teams</Link>
        <Link to="/repository-requests">Repository requests</Link>
      </nav>
      {adding ? (
        <section className="panel panel-body">
          <h2>Create a team</h2>
          <TeamEditor done={() => setAdding(false)} />
        </section>
      ) : null}
      <form
        className="search-form toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          const next = new URLSearchParams(params);
          next.set("search", draft.trim());
          next.delete("after_id");
          setParams(next);
        }}
      >
        <label className="field grow">
          Find a team
          <input
            type="search"
            maxLength={80}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Search team names"
          />
        </label>
        <button className="secondary">Search</button>
      </form>
      <Freshness query={query} />
      {assignRepository ? (
        <p className="notice">
          Choose the owning team for <strong>{repositoryName}</strong>. This
          assigns retained review history; it leaves GitHub grants and review
          activation as they are.{" "}
          <button className="text-button" onClick={clearAssignment}>
            Cancel assignment
          </button>
        </p>
      ) : null}
      {query.data?.items.length ? (
        <div
          className="panel table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Teams"
        >
          <table>
            <thead>
              <tr>
                <th>Team</th>
                <th>Your access</th>
                <th className="numeric">Repositories</th>
                <th className="numeric">Members</th>
                <th className="numeric">Pending requests</th>
                {assignRepository ? <th>Repository assignment</th> : null}
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((team) => (
                <tr key={team.id}>
                  <th scope="row">
                    <Link to={`/teams/${team.id}?team_id=${team.id}`}>
                      {team.name}
                    </Link>
                    {team.description ? (
                      <span className="subtext">{team.description}</span>
                    ) : null}
                  </th>
                  <td>
                    {isAdmin(scope.current.role)
                      ? "Platform admin"
                      : team.role === "maintainer"
                        ? "Maintainer"
                        : "Viewer"}
                  </td>
                  <td className="numeric">{team.repository_count}</td>
                  <td className="numeric">{team.member_count}</td>
                  <td className="numeric">
                    {team.pending_requests ? (
                      <Link
                        to={`/teams/${team.id}?team_id=${team.id}&tab=requests`}
                      >
                        {team.pending_requests}
                      </Link>
                    ) : (
                      "0"
                    )}
                  </td>
                  {assignRepository ? (
                    <td>
                      <ReasonAction
                        label="Assign to this team"
                        method="PUT"
                        path={`/api/repository-ownership/${assignRepository}`}
                        body={{ team_id: team.id, expected_team_id: null }}
                        description={`Assign ${repositoryName} and its retained review history to ${team.name}.`}
                        done={clearAssignment}
                      />
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : query.data ? (
        <Empty title={search ? "No matching teams" : "No teams yet"}>
          {search
            ? "Try another team name."
            : isAdmin(scope.current.role)
              ? "Create a team, add its maintainers, then approve their repository requests."
              : "Ask an administrator or team maintainer to add your account to a team."}
        </Empty>
      ) : null}
      {query.data && (after !== "0" || query.data.next_after_id) ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={after === "0"}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("after_id");
              setParams(next);
            }}
          >
            First page
          </button>
          <span>{query.data.total} teams</span>
          <button
            className="secondary"
            disabled={!query.data.next_after_id}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("after_id", String(query.data?.next_after_id));
              setParams(next);
            }}
          >
            Next teams
          </button>
        </div>
      ) : null}
    </>
  );
}

function Members({ team, maintain }: { team: Team; maintain: boolean }) {
  const scope = useScope();
  const refresh = useTeamRefresh();
  const [offset, setOffset] = useState(0);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<TeamRole>("viewer");
  const [reason, setReason] = useState("");
  const query = useQuery({
    queryKey: ["team-members", team.id, offset, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamMemberPage>(
        `/api/teams/${team.id}/members?offset=${offset}`,
        signal,
      ),
  });
  const save = useMutation({
    mutationFn: () =>
      write(`/api/teams/${team.id}/members`, "PUT", {
        email,
        role,
        reason,
      } satisfies components["schemas"]["TeamMemberUpdate"]),
    onSuccess: async () => {
      setEmail("");
      setReason("");
      await refresh();
    },
  });
  return (
    <>
      <h2>Members</h2>
      <p>
        Viewers can read this team's review activity. Maintainers can also
        manage members, request repositories, and act on reviews.
      </p>
      {maintain ? (
        <details className="section-disclosure">
          <summary>Add or change a member</summary>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              save.mutate();
            }}
          >
            <div className="form-fields">
              <label className="field grow">
                Account email
                <input
                  type="email"
                  required
                  maxLength={320}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                />
                <span className="field-help">
                  Use an existing active account.
                </span>
              </label>
              <label className="field">
                Team role
                <select
                  value={role}
                  onChange={(event) => setRole(event.target.value as TeamRole)}
                >
                  <option value="viewer">Viewer</option>
                  <option value="maintainer">Maintainer</option>
                </select>
              </label>
            </div>
            <label className="field">
              Reason
              <textarea
                required
                maxLength={500}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            {save.isError ? (
              <p className="notice error" role="alert">
                {save.error.message}
              </p>
            ) : null}
            {save.isSuccess ? <p role="status">Membership saved.</p> : null}
            <button disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save membership"}
            </button>
          </form>
        </details>
      ) : null}
      <Freshness query={query} />
      {query.data?.items.length ? (
        <div
          className="panel table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Team members"
        >
          <table>
            <thead>
              <tr>
                <th>Account</th>
                <th>Role</th>
                <th>Access</th>
                {maintain ? <th>Action</th> : null}
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((member) => (
                <tr key={member.user_id}>
                  <th scope="row">
                    {member.email}
                    {member.user_id === scope.current.id ? (
                      <span className="subtext">You</span>
                    ) : null}
                  </th>
                  <td>
                    {member.role === "maintainer" ? "Maintainer" : "Viewer"}
                  </td>
                  <td>{member.active ? "Active" : "Account disabled"}</td>
                  {maintain ? (
                    <td>
                      <ReasonAction
                        label="Remove member"
                        path={`/api/teams/${team.id}/members/${member.user_id}/remove`}
                        description={`Remove ${member.email} from ${team.name}. Their access through other teams is retained.`}
                        danger
                      />
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : query.data ? (
        <Empty title="No members yet">
          {maintain
            ? "Add an existing account by email. A maintainer can then manage this team."
            : "A maintainer can add accounts to this team."}
        </Empty>
      ) : null}
      {offset || query.data?.next_offset ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={!offset}
            onClick={() => setOffset(Math.max(0, offset - 50))}
          >
            Previous
          </button>
          <button
            className="secondary"
            disabled={!query.data?.next_offset}
            onClick={() => setOffset(query.data?.next_offset ?? 0)}
          >
            Next
          </button>
        </div>
      ) : null}
    </>
  );
}

function TeamRepositories({
  team,
  maintain,
}: {
  team: Team;
  maintain: boolean;
}) {
  const scope = useScope();
  const refresh = useTeamRefresh();
  const [after, setAfter] = useState(0);
  const [repository, setRepository] = useState("");
  const [reason, setReason] = useState("");
  const [destination, setDestination] = useState("");
  const [teamSearch, setTeamSearch] = useState("");
  const admin = isAdmin(scope.current.role);
  const query = useQuery({
    queryKey: ["team-repositories", team.id, after, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamRepositoryPage>(
        `/api/teams/${team.id}/repositories?after_id=${after}`,
        signal,
      ),
  });
  const destinations = useQuery({
    queryKey: ["teams", "destination", teamSearch, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPage>(
        `/api/teams?limit=20&search=${encodeURIComponent(teamSearch)}`,
        signal,
      ),
    enabled: admin && teamSearch.length >= 2,
  });
  const submit = useMutation({
    mutationFn: () =>
      write(`/api/teams/${team.id}/repository-requests`, "POST", {
        repository,
        reason,
      } satisfies components["schemas"]["RepositorySubmission"]),
    onSuccess: async () => {
      setRepository("");
      setReason("");
      await refresh();
    },
  });
  return (
    <>
      <h2>Repositories</h2>
      <p>
        Each repository belongs to one team. Approval verifies its GitHub App
        grant before enabling reviews.
      </p>
      {maintain ? (
        <details className="section-disclosure">
          <summary>Request a repository</summary>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              submit.mutate();
            }}
          >
            <label className="field">
              GitHub repository
              <input
                required
                placeholder="owner/repository or https://github.com/owner/repository"
                maxLength={260}
                value={repository}
                onChange={(event) => setRepository(event.target.value)}
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
            <p className="field-help">
              A platform administrator approves requests. The GitHub App must
              already have access to the repository.
            </p>
            {submit.isError ? (
              <p className="notice error" role="alert">
                {submit.error.message}
              </p>
            ) : null}
            {submit.isSuccess ? (
              <p role="status">
                Request submitted. Follow its status in{" "}
                <Link to={`/teams/${team.id}?tab=requests&team_id=${team.id}`}>
                  Repository requests
                </Link>
                .
              </p>
            ) : null}
            <button disabled={submit.isPending}>
              {submit.isPending ? "Submitting…" : "Submit request"}
            </button>
          </form>
        </details>
      ) : null}
      {admin ? (
        <details className="section-disclosure">
          <summary>Transfer a repository to another team</summary>
          <p>
            Find the destination team, then choose Transfer on a repository
            below. Existing review history follows repository ownership.
          </p>
          <label className="field">
            Find destination team
            <input
              type="search"
              placeholder="Type at least two characters"
              maxLength={80}
              value={teamSearch}
              onChange={(event) => {
                setTeamSearch(event.target.value);
                setDestination("");
              }}
            />
          </label>
          {teamSearch.length >= 2 ? (
            <>
              <Freshness query={destinations} />
              <label className="field">
                Destination
                <select
                  value={destination}
                  onChange={(event) => setDestination(event.target.value)}
                >
                  <option value="">Choose a team</option>
                  {destinations.data?.items
                    .filter((item) => item.id !== team.id)
                    .map((item) => (
                      <option value={item.id} key={item.id}>
                        {item.name}
                      </option>
                    ))}
                </select>
              </label>
              {destinations.data?.next_after_id ? (
                <p className="field-help">
                  Refine the name to find more teams.
                </p>
              ) : null}
            </>
          ) : null}
        </details>
      ) : null}
      <Freshness query={query} />
      {query.data?.items.length ? (
        <div
          className="panel table-scroll"
          tabIndex={0}
          role="region"
          aria-label="Team repositories"
        >
          <table>
            <thead>
              <tr>
                <th>Repository</th>
                <th>Review Agent</th>
                <th>GitHub access</th>
                <th>Profile</th>
                {admin ? <th>Actions</th> : null}
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((repo) => (
                <tr key={repo.repository_id}>
                  <th scope="row">
                    <Link
                      to={`/history?repository=${encodeURIComponent(repo.repository)}&team_id=${team.id}`}
                    >
                      {repo.repository}
                    </Link>
                    <span className="subtext">
                      Assigned {time(repo.assigned_at)}
                    </span>
                  </th>
                  <td>{repo.enabled ? "Enabled" : "Disabled"}</td>
                  <td>{repo.access?.replaceAll("_", " ") ?? "Not granted"}</td>
                  <td>{repo.profile ?? "—"}</td>
                  {admin ? (
                    <td>
                      {destination ? (
                        <ReasonAction
                          label="Transfer"
                          method="PUT"
                          path={`/api/repository-ownership/${repo.repository_id}`}
                          body={{
                            team_id: Number(destination),
                            expected_team_id: team.id,
                          }}
                          description={`Transfer ${repo.repository} and its history to ${destinations.data?.items.find((item) => String(item.id) === destination)?.name ?? "the selected team"}. This team will lose access.`}
                        />
                      ) : null}
                      <ReasonAction
                        label="Remove repository"
                        path={`/api/teams/${team.id}/repositories/${repo.repository_id}/remove`}
                        description={`Disable reviews for ${repo.repository} and remove its team ownership. Stored review history is retained for platform administrators.`}
                        danger
                      />
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : query.data ? (
        <Empty title="No repositories assigned">
          {maintain
            ? "Request a repository above. An administrator can then verify access and approve it."
            : "A team maintainer can request repository access."}
        </Empty>
      ) : null}
      {after || query.data?.next_after_id ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={!after}
            onClick={() => setAfter(0)}
          >
            First page
          </button>
          <button
            className="secondary"
            disabled={!query.data?.next_after_id}
            onClick={() => setAfter(query.data?.next_after_id ?? 0)}
          >
            Next
          </button>
        </div>
      ) : null}
    </>
  );
}

export function RepositoryRequests({
  team,
  maintain = false,
}: {
  team?: Team;
  maintain?: boolean;
}) {
  const scope = useScope();
  const [status, setStatus] = useState("pending");
  const [before, setBefore] = useState<number | null>(null);
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (before) params.set("before_id", String(before));
  if (team) params.set("team_id", String(team.id));
  const query = useQuery({
    queryKey: [
      "repository-requests",
      team?.id,
      status,
      before,
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<RepositoryRequestPage>(`/api/repository-requests?${params}`, signal),
  });
  const admin = isAdmin(scope.current.role);
  useEffect(() => {
    if (!team) document.title = "Review Agent · Repository requests";
  }, [team]);
  return (
    <>
      {!team ? (
        <>
          <div className="page-heading">
            <div>
              <h1>Repository requests</h1>
              <p>
                {admin
                  ? "Platform administration · Verify GitHub access and assign repositories to teams."
                  : "Track repository requests for your teams."}
              </p>
            </div>
            <Link className="button secondary" to="/teams">
              Teams
            </Link>
          </div>
        </>
      ) : (
        <h2>Repository requests</h2>
      )}
      <div className="toolbar">
        <label className="field">
          Status
          <select
            value={status}
            onChange={(event) => {
              setStatus(event.target.value);
              setBefore(null);
            }}
          >
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="withdrawn">Withdrawn</option>
            <option value="">All requests</option>
          </select>
        </label>
        <span>{query.data ? `${query.data.pending} pending` : ""}</span>
      </div>
      <Freshness query={query} />
      {query.data?.items.length ? (
        <div className="request-list">
          {query.data.items.map((request) => (
            <article className="request-row" key={request.id}>
              <div className="panel-heading">
                <div>
                  <h3>{request.repository_name}</h3>
                  <span className="subtext">
                    <Link
                      to={`/teams/${request.team_id}?team_id=${request.team_id}`}
                    >
                      {request.team_name}
                    </Link>{" "}
                    · Requested {time(request.submitted_at)}
                  </span>
                </div>
                <span
                  className={`status ${request.status === "approved" ? "published" : request.status === "pending" ? "queued" : "superseded"}`}
                >
                  {request.status}
                </span>
              </div>
              <p>{request.reason}</p>
              {request.decision_reason ? (
                <p>
                  Decision: {request.decision_reason} ·{" "}
                  {time(request.decided_at)}
                </p>
              ) : null}
              {request.status === "pending" ? (
                <div className="request-actions">
                  {admin ? (
                    <>
                      <ReasonAction
                        label="Approve repository"
                        path={`/api/repository-requests/${request.id}/approve`}
                        description={`Verify the current GitHub App grant, assign ${request.repository_name} to ${request.team_name}, and enable reviews with the deployment's default profile.`}
                      />
                      <ReasonAction
                        label="Reject request"
                        path={`/api/repository-requests/${request.id}/reject`}
                        description="Record why this repository request cannot be approved."
                      />
                    </>
                  ) : null}
                  {maintain || admin ? (
                    <ReasonAction
                      label="Withdraw request"
                      path={`/api/repository-requests/${request.id}/withdraw`}
                      description="Close this pending request. A new request can be submitted later."
                    />
                  ) : null}
                </div>
              ) : null}
            </article>
          ))}
        </div>
      ) : query.data ? (
        <Empty title="No requests in this view">
          {status === "pending"
            ? "There are no repository requests waiting for approval."
            : "Choose another status to see earlier requests."}
        </Empty>
      ) : null}
      {before || query.data?.next_before_id ? (
        <div className="pagination">
          <button
            className="secondary"
            disabled={!before}
            onClick={() => setBefore(null)}
          >
            Latest requests
          </button>
          <button
            className="secondary"
            disabled={!query.data?.next_before_id}
            onClick={() => setBefore(query.data?.next_before_id ?? null)}
          >
            Older requests
          </button>
        </div>
      ) : null}
    </>
  );
}

function TeamModelEditor({ policy }: { policy: ModelPolicy }) {
  const scope = useScope();
  const client = useQueryClient();
  const [selected, setSelected] = useState<ModelConnection>(policy.connection);
  const [after, setAfter] = useState(0);
  const [route, setRoute] = useState(
    policy.provider === null
      ? -1
      : policy.connection.allowed_routes.findIndex(
          (choice) =>
            choice.provider === policy.provider &&
            choice.model === policy.model,
        ),
  );
  const [effort, setEffort] = useState(policy.reasoning_effort ?? "");
  const [maxConcurrency, setMaxConcurrency] = useState(policy.max_concurrency);
  const [reason, setReason] = useState("");
  const admin = isAdmin(scope.current.role);
  const connections = useQuery({
    queryKey: [
      "model-connections",
      "team-options",
      policy.team_id,
      after,
      "scoped",
      scope.key,
    ],
    queryFn: ({ signal }) =>
      read<ModelConnectionPage>(
        `/api/model-connections?team_id=${policy.team_id}&after_id=${after}`,
        signal,
      ),
    enabled: admin,
  });
  const choice = route >= 0 ? selected.allowed_routes[route] : undefined;
  const save = useMutation({
    mutationFn: () =>
      write(`/api/teams/${policy.team_id}/model-policy`, "PUT", {
        connection_id: selected.runtime_key === "shared" ? null : selected.id,
        provider: choice?.provider ?? null,
        model: choice?.model ?? null,
        reasoning_effort: choice ? effort : null,
        max_concurrency: maxConcurrency,
        expected_revision: policy.revision,
        reason,
      } satisfies components["schemas"]["TeamModelUpdate"]),
    onSuccess: async () => {
      await Promise.all(
        ["team-model-policy", "model-connections", "audit"].map((key) =>
          client.invalidateQueries({ queryKey: [key] }),
        ),
      );
      setReason("");
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
      {admin ? (
        <>
          <Freshness query={connections} />
          <label className="field">
            Assigned connection
            <select
              value={selected.id}
              onChange={(event) => {
                const next = connections.data?.items.find(
                  (connection) => connection.id === Number(event.target.value),
                );
                if (next) {
                  setSelected(next);
                  setRoute(-1);
                  setEffort("");
                }
              }}
            >
              {!connections.data?.items.some(
                (connection) => connection.id === selected.id,
              ) ? (
                <option value={selected.id}>{selected.name}</option>
              ) : null}
              {connections.data?.items.map((connection) => (
                <option value={connection.id} key={connection.id}>
                  {connection.name}
                  {connection.state !== "enabled"
                    ? " (paused or unavailable)"
                    : ""}
                </option>
              ))}
            </select>
          </label>
          {after > 0 || connections.data?.next_after_id ? (
            <div className="pagination">
              <button
                type="button"
                className="secondary"
                disabled={after === 0}
                onClick={() => setAfter(0)}
              >
                First connections
              </button>
              <button
                type="button"
                className="secondary"
                disabled={!connections.data?.next_after_id}
                onClick={() => setAfter(connections.data?.next_after_id ?? 0)}
              >
                Next connections
              </button>
            </div>
          ) : null}
        </>
      ) : null}
      <div className="form-fields">
        <label className="field grow">
          Model
          <select
            value={route}
            onChange={(event) => {
              const index = Number(event.target.value);
              setRoute(index);
              setEffort(
                selected.allowed_routes[index]?.reasoning_efforts[0] ?? "",
              );
            }}
          >
            <option value={-1}>Inherit deployment defaults</option>
            {selected.allowed_routes.map((choice, index) => (
              <option value={index} key={`${choice.provider}:${choice.model}`}>
                {choice.provider === "openai-codex"
                  ? "OpenAI Codex"
                  : "Anthropic"}{" "}
                · {choice.model}
              </option>
            ))}
          </select>
        </label>
        {choice ? (
          <label className="field">
            Reasoning
            <select
              required
              value={effort}
              onChange={(event) => setEffort(event.target.value)}
            >
              {choice.reasoning_efforts.map((effort) => (
                <option value={effort} key={effort}>
                  {effort}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>
      {!selected.allowed_routes.length ? (
        <p className="field-help">
          This connection currently allows deployment defaults only. An
          administrator can add model choices.
        </p>
      ) : null}
      {admin ? (
        <label className="field">
          Maximum concurrent team reviews
          <input
            type="number"
            required
            min={1}
            max={2147483647}
            step={1}
            value={maxConcurrency}
            onChange={(event) => setMaxConcurrency(event.target.valueAsNumber)}
          />
        </label>
      ) : null}
      <label className="field">
        Reason
        <textarea
          required
          maxLength={500}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <p className="field-help">
        Model changes apply to newly admitted reviews. Queued and running
        reviews keep their original account and model. Capacity limits apply to
        new claims across all workers and connections; reviews already claimed
        can finish.
      </p>
      {save.isError ? (
        <p className="notice error" role="alert">
          {save.error.message}
        </p>
      ) : null}
      {save.isSuccess ? <p role="status">Team model policy saved.</p> : null}
      <button disabled={save.isPending}>
        {save.isPending ? "Saving…" : "Save model policy"}
      </button>
    </form>
  );
}

function TeamModels({ team, maintain }: { team: Team; maintain: boolean }) {
  const scope = useScope();
  const query = useQuery({
    queryKey: ["team-model-policy", team.id, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<ModelPolicy>(`/api/teams/${team.id}/model-policy`, signal),
  });
  const policy = query.data;
  return (
    <section className="section">
      <h2>Model and account</h2>
      <Freshness query={query} />
      {policy ? (
        <>
          <dl className="detail-list">
            <dt>Connection</dt>
            <dd>
              <Link
                to={`/model-connections/${policy.connection.id}?team_id=${team.id}`}
              >
                {policy.connection.name}
              </Link>{" "}
              · {policy.connection.team_id ? "Dedicated" : "Shared"}
            </dd>
            <dt>Model</dt>
            <dd>
              {policy.effective_provider === "openai-codex"
                ? "OpenAI Codex"
                : "Anthropic"}{" "}
              · {policy.effective_model}
            </dd>
            <dt>Reasoning</dt>
            <dd>{policy.effective_reasoning_effort}</dd>
            <dt>Team capacity</dt>
            <dd>
              {policy.max_concurrency} concurrent reviews across connections
            </dd>
            <dt>Connection capacity</dt>
            <dd>
              {policy.connection.max_concurrency} concurrent reviews shared by
              its teams
            </dd>
            <dt>Source</dt>
            <dd>
              {policy.provider === null
                ? "Inherited from deployment defaults"
                : "Team model policy"}
            </dd>
          </dl>
          {policy.connection.state !== "enabled" ? (
            <p className="notice">
              This connection is paused or needs attention. Open the connection
              to check its status.
            </p>
          ) : null}
          {maintain ? (
            <details className="section-disclosure">
              <summary>Change the team's model policy</summary>
              <TeamModelEditor
                key={`${policy.revision}:${policy.connection.revision}`}
                policy={policy}
              />
            </details>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

export function TeamDetail() {
  const scope = useScope();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "repositories";
  const query = scope.teamQuery;
  useEffect(() => {
    if (query.data) document.title = `Review Agent · ${query.data.name}`;
  }, [query.data]);
  const team = query.data;
  const maintain = isAdmin(scope.current.role) || team?.role === "maintainer";
  return (
    <>
      <Freshness query={query} />
      {team ? (
        <>
          <div className="page-heading">
            <div>
              <h1>{team.name}</h1>
              <p>
                {team.description ||
                  "Team repositories, membership, and access history."}
              </p>
            </div>
            <Link className="button secondary" to={`/?team_id=${team.id}`}>
              View team activity
            </Link>
          </div>
          <nav className="page-tabs" aria-label="Team views">
            {[
              ["repositories", "Repositories"],
              ["members", "Members"],
              ["models", "Model and account"],
              [
                "requests",
                `Requests${team.pending_requests ? ` (${team.pending_requests})` : ""}`,
              ],
              ...(isAdmin(scope.current.role) ? [["audit", "Audit log"]] : []),
            ].map(([key, label]) => (
              <button
                className={tab === key ? "active" : ""}
                aria-current={tab === key ? "page" : undefined}
                key={key}
                onClick={() => {
                  const next = new URLSearchParams(params);
                  next.set("tab", key ?? "repositories");
                  next.set("team_id", String(team.id));
                  setParams(next);
                }}
              >
                {label}
              </button>
            ))}
          </nav>
          {tab === "members" ? (
            <Members team={team} maintain={maintain} />
          ) : tab === "models" ? (
            <TeamModels team={team} maintain={maintain} />
          ) : tab === "requests" ? (
            <RepositoryRequests team={team} maintain={maintain} />
          ) : tab === "audit" && isAdmin(scope.current.role) ? (
            <AuditLog teamId={team.id} />
          ) : (
            <TeamRepositories team={team} maintain={maintain} />
          )}
          {isAdmin(scope.current.role) ? (
            <details className="section-disclosure">
              <summary>Edit team details</summary>
              <TeamEditor key={team.revision} team={team} />
            </details>
          ) : null}
        </>
      ) : null}
    </>
  );
}
