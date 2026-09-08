import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Selector } from "@astryxdesign/core/Selector";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import type {
  RepositoryRequestPage,
  Team,
  TeamMemberPage,
  TeamPage,
  TeamRepositoryPage,
} from "./api";
import { read, write } from "./api";
import type { components } from "./api.generated";
import { AuditLog } from "./audit";
import { isAdmin, ScopedLink as Link, ScopedAnchor, useScope } from "./scope";
import { Empty, Form, Freshness, time } from "./ui";

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
    <VStack gap={3}>
      <Button
        label={label}
        variant={danger ? "destructive" : "secondary"}
        type="button"

        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
          action.reset();
        }}
      />
      {open ? (
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            action.mutate();
          }}
        >
          <Text as="p">{description}</Text>

          <TextArea
            label={"Reason"}
            hasAutoFocus={true}
            isRequired={true}
            maxLength={500}
            value={reason}
            onChange={(value) => setReason(value.slice(0, 500))}
            {...({
              required: true,
            } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
          />

          {action.isError ? (
            <Text as="p" role="alert">
              {action.error.message}
            </Text>
          ) : null}
          <HStack gap={3} wrap="wrap" vAlign="center">
            <Button
              label={action.isPending ? "Saving…" : label}
              variant="primary"
              type="submit"
              isDisabled={action.isPending || !reason.trim()}
            />
            <Button
              label={"Cancel"}
              variant="secondary"
              type="button"

              isDisabled={action.isPending}
              onClick={() => setOpen(false)}
            />
          </HStack>
        </Form>
      ) : null}
    </VStack>
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
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <Grid gap={4} columns={{ minWidth: 240, max: 4, repeat: "fit" }}>
        <TextInput
          label={"Team name"}
          hasAutoFocus={true}
          isRequired={true}
          value={name}
          onChange={(value) => setName(value)}
          {...({
            required: true,
            maxLength: 80,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />

        <TextInput
          label={"Description"}
          value={description}
          onChange={(value) => setDescription(value)}
          {...({
            maxLength: 500,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />
      </Grid>

      <TextArea
        label={"Reason"}
        isRequired={true}
        maxLength={500}
        value={reason}
        onChange={(value) => setReason(value.slice(0, 500))}
        {...({
          required: true,
        } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
      />

      {save.isError ? (
        <Text as="p" role="alert">
          {save.error.message}
        </Text>
      ) : null}
      {save.isSuccess ? (
        <Text as="p" role="status">
          Team saved.
        </Text>
      ) : null}
      <Button
        label={String(
          save.isPending ? "Saving…" : team ? "Save team" : "Create team",
        )}
        variant="primary"
        type="submit"
        isDisabled={save.isPending}
      />
    </Form>
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
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Teams</Heading>
          <Text as="p">
            {isAdmin(scope.current.role)
              ? "Platform administration · Manage team membership and repository ownership."
              : "Your teams, repositories, and access."}
          </Text>
        </VStack>
        {isAdmin(scope.current.role) ? (
          <Button
            label={String(adding ? "Close form" : "Create team")}
            variant="primary"
            type="submit"
            onClick={() => setAdding(!adding)}
            aria-expanded={adding}
          />
        ) : null}
      </HStack>
      <TabList
        aria-label="Team administration"
        value="/teams"
        onChange={() => {}}
        hasDivider
      >
        <Tab value="/teams" href="/teams" as={ScopedAnchor} label="Teams" />
        <Tab
          value="/repository-requests"
          href="/repository-requests"
          as={ScopedAnchor}
          label="Repository requests"
        />
      </TabList>
      {adding ? (
        <VStack gap={4} as="section">
          <Heading level={2}>Create a team</Heading>
          <TeamEditor done={() => setAdding(false)} />
        </VStack>
      ) : null}
      <HStack
        gap={3}
        wrap="wrap"
        vAlign="center"
        as="form"

        onSubmit={(event) => {
          event.preventDefault();
          const next = new URLSearchParams(params);
          next.set("search", draft.trim());
          next.delete("after_id");
          setParams(next);
        }}
      >
        <TextInput
          label={"Find a team"}
          value={draft}
          onChange={(value) => setDraft(value)}
          placeholder="Search team names"
          {...({
            maxLength: 80,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />

        <Button label={"Search"} variant="secondary" type="submit" />
      </HStack>
      <Freshness query={query} />
      {assignRepository ? (
        <Text as="p">
          Choose the owning team for <strong>{repositoryName}</strong>. This
          assigns retained review history; it leaves GitHub grants and review
          activation as they are.{" "}
          <Button
            label={"Cancel assignment"}
            variant="primary"
            type="submit"
            onClick={clearAssignment}
          />
        </Text>
      ) : null}
      {query.data?.items.length ? (
        <VStack
          gap={4}

          tabIndex={0}
          role="region"
          aria-label="Teams"
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Team</TableHeaderCell>
                <TableHeaderCell>Your access</TableHeaderCell>
                <TableHeaderCell>Repositories</TableHeaderCell>
                <TableHeaderCell>Members</TableHeaderCell>
                <TableHeaderCell>Pending requests</TableHeaderCell>
                {assignRepository ? (
                  <TableHeaderCell>Repository assignment</TableHeaderCell>
                ) : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.map((team) => (
                <TableRow key={team.id}>
                  <TableHeaderCell scope="row">
                    <Link to={`/teams/${team.id}?team_id=${team.id}`}>
                      {team.name}
                    </Link>
                    {team.description ? (
                      <Text color="secondary" display="block" type="supporting">
                        {team.description}
                      </Text>
                    ) : null}
                  </TableHeaderCell>
                  <TableCell>
                    {isAdmin(scope.current.role)
                      ? "Platform admin"
                      : team.role === "maintainer"
                        ? "Maintainer"
                        : "Viewer"}
                  </TableCell>
                  <TableCell>{team.repository_count}</TableCell>
                  <TableCell>{team.member_count}</TableCell>
                  <TableCell>
                    {team.pending_requests ? (
                      <Link
                        to={`/teams/${team.id}?team_id=${team.id}&tab=requests`}
                      >
                        {team.pending_requests}
                      </Link>
                    ) : (
                      "0"
                    )}
                  </TableCell>
                  {assignRepository ? (
                    <TableCell>
                      <ReasonAction
                        label="Assign to this team"
                        method="PUT"
                        path={`/api/repository-ownership/${assignRepository}`}
                        body={{ team_id: team.id, expected_team_id: null }}
                        description={`Assign ${repositoryName} and its retained review history to ${team.name}.`}
                        done={clearAssignment}
                      />
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </VStack>
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
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"First page"}
            variant="secondary"
            type="submit"

            isDisabled={after === "0"}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("after_id");
              setParams(next);
            }}
          />
          <Text>{query.data.total} teams</Text>
          <Button
            label={"Next teams"}
            variant="secondary"
            type="submit"

            isDisabled={!query.data.next_after_id}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("after_id", String(query.data?.next_after_id));
              setParams(next);
            }}
          />
        </HStack>
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
      <Heading level={2}>Members</Heading>
      <Text as="p">
        Viewers can read this team's review activity. Maintainers can also
        manage members, request repositories, and act on reviews.
      </Text>
      {maintain ? (
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Add or change a member
            </HStack>
          }
        >
          <VStack gap={4}>
            <Form
              onSubmit={(event) => {
                event.preventDefault();
                save.mutate();
              }}
            >
              <Grid gap={4} columns={{ minWidth: 240, max: 4, repeat: "fit" }}>
                <VStack gap={2}>
                  <TextInput
                    label={"Account email"}
                    type="email"
                    isRequired={true}
                    value={email}
                    onChange={(value) => setEmail(value)}
                    {...({
                      required: true,
                      maxLength: 320,
                    } satisfies InputHTMLAttributes<HTMLInputElement>)}
                  />
                  <Text color="secondary">Use an existing active account.</Text>
                </VStack>
                <Selector
                  label={"Team role"}
                  options={[
                    { value: "viewer", label: "Viewer" },
                    { value: "maintainer", label: "Maintainer" },
                  ]}
                  value={role}
                  onChange={(value) => setRole(value as TeamRole)}
                />
              </Grid>

              <TextArea
                label={"Reason"}
                isRequired={true}
                maxLength={500}
                value={reason}
                onChange={(value) => setReason(value.slice(0, 500))}
                {...({
                  required: true,
                } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
              />

              {save.isError ? (
                <Text as="p" role="alert">
                  {save.error.message}
                </Text>
              ) : null}
              {save.isSuccess ? (
                <Text as="p" role="status">
                  Membership saved.
                </Text>
              ) : null}
              <Button
                label={String(save.isPending ? "Saving…" : "Save membership")}
                variant="primary"
                type="submit"
                isDisabled={save.isPending}
              />
            </Form>
          </VStack>
        </Collapsible>
      ) : null}
      <Freshness query={query} />
      {query.data?.items.length ? (
        <VStack
          gap={4}

          tabIndex={0}
          role="region"
          aria-label="Team members"
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Account</TableHeaderCell>
                <TableHeaderCell>Role</TableHeaderCell>
                <TableHeaderCell>Access</TableHeaderCell>
                {maintain ? <TableHeaderCell>Action</TableHeaderCell> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.map((member) => (
                <TableRow key={member.user_id}>
                  <TableHeaderCell scope="row">
                    {member.email}
                    {member.user_id === scope.current.id ? (
                      <Text color="secondary" display="block" type="supporting">
                        You
                      </Text>
                    ) : null}
                  </TableHeaderCell>
                  <TableCell>
                    {member.role === "maintainer" ? "Maintainer" : "Viewer"}
                  </TableCell>
                  <TableCell>
                    {member.active ? "Active" : "Account disabled"}
                  </TableCell>
                  {maintain ? (
                    <TableCell>
                      <ReasonAction
                        label="Remove member"
                        path={`/api/teams/${team.id}/members/${member.user_id}/remove`}
                        description={`Remove ${member.email} from ${team.name}. Their access through other teams is retained.`}
                        danger
                      />
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </VStack>
      ) : query.data ? (
        <Empty title="No members yet">
          {maintain
            ? "Add an existing account by email. A maintainer can then manage this team."
            : "A maintainer can add accounts to this team."}
        </Empty>
      ) : null}
      {offset || query.data?.next_offset ? (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"Previous"}
            variant="secondary"
            type="submit"

            isDisabled={!offset}
            onClick={() => setOffset(Math.max(0, offset - 50))}
          />
          <Button
            label={"Next"}
            variant="secondary"
            type="submit"

            isDisabled={!query.data?.next_offset}
            onClick={() => setOffset(query.data?.next_offset ?? 0)}
          />
        </HStack>
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
      <Heading level={2}>Repositories</Heading>
      <Text as="p">
        Each repository belongs to one team. Approval verifies its GitHub App
        grant before enabling reviews.
      </Text>
      {maintain ? (
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Request a repository
            </HStack>
          }
        >
          <VStack gap={4}>
            <Form
              onSubmit={(event) => {
                event.preventDefault();
                submit.mutate();
              }}
            >
              <TextInput
                label={"GitHub repository"}
                isRequired={true}
                placeholder="owner/repository or https://github.com/owner/repository"
                value={repository}
                onChange={(value) => setRepository(value)}
                {...({
                  required: true,
                  maxLength: 260,
                } satisfies InputHTMLAttributes<HTMLInputElement>)}
              />

              <TextArea
                label={"Reason"}
                isRequired={true}
                maxLength={500}
                value={reason}
                onChange={(value) => setReason(value.slice(0, 500))}
                {...({
                  required: true,
                } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
              />

              <Text as="p" color="secondary">
                A platform administrator approves requests. The GitHub App must
                already have access to the repository.
              </Text>
              {submit.isError ? (
                <Text as="p" role="alert">
                  {submit.error.message}
                </Text>
              ) : null}
              {submit.isSuccess ? (
                <Text as="p" role="status">
                  Request submitted. Follow its status in{" "}
                  <Link
                    to={`/teams/${team.id}?tab=requests&team_id=${team.id}`}
                  >
                    Repository requests
                  </Link>
                  .
                </Text>
              ) : null}
              <Button
                label={String(
                  submit.isPending ? "Submitting…" : "Submit request",
                )}
                variant="primary"
                type="submit"
                isDisabled={submit.isPending}
              />
            </Form>
          </VStack>
        </Collapsible>
      ) : null}
      {admin ? (
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Transfer a repository to another team
            </HStack>
          }
        >
          <VStack gap={4}>
            <Text as="p">
              Find the destination team, then choose Transfer on a repository
              below. Existing review history follows repository ownership.
            </Text>

            <TextInput
              label={"Find destination team"}
              placeholder="Type at least two characters"
              value={teamSearch}
              onChange={(value) => {
                setTeamSearch(value);
                setDestination("");
              }}
              {...({
                maxLength: 80,
              } satisfies InputHTMLAttributes<HTMLInputElement>)}
            />

            {teamSearch.length >= 2 ? (
              <>
                <Freshness query={destinations} />
                <Selector
                  label={"Destination"}
                  options={[
                    { value: "", label: "Choose a team" },
                    destinations.data?.items
                      .filter((item) => item.id !== team.id)
                      .map((item) => ({
                        value: String(item.id),
                        label: item.name,
                      })),
                  ]
                    .flat()
                    .filter((option) => option != null)}
                  value={destination}
                  onChange={(value) => setDestination(value)}
                />
                {destinations.data?.next_after_id ? (
                  <Text as="p" color="secondary">
                    Refine the name to find more teams.
                  </Text>
                ) : null}
              </>
            ) : null}
          </VStack>
        </Collapsible>
      ) : null}
      <Freshness query={query} />
      {query.data?.items.length ? (
        <VStack
          gap={4}

          tabIndex={0}
          role="region"
          aria-label="Team repositories"
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHeaderCell>Repository</TableHeaderCell>
                <TableHeaderCell>Review Agent</TableHeaderCell>
                <TableHeaderCell>GitHub access</TableHeaderCell>
                <TableHeaderCell>Profile</TableHeaderCell>
                {admin ? <TableHeaderCell>Actions</TableHeaderCell> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.map((repo) => (
                <TableRow key={repo.repository_id}>
                  <TableHeaderCell scope="row">
                    <Link
                      to={`/history?repository=${encodeURIComponent(repo.repository)}&team_id=${team.id}`}
                    >
                      {repo.repository}
                    </Link>
                    <Text color="secondary" display="block" type="supporting">
                      Assigned {time(repo.assigned_at)}
                    </Text>
                  </TableHeaderCell>
                  <TableCell>{repo.enabled ? "Enabled" : "Disabled"}</TableCell>
                  <TableCell>
                    {repo.access?.replaceAll("_", " ") ?? "Not granted"}
                  </TableCell>
                  <TableCell>{repo.profile ?? "—"}</TableCell>
                  {admin ? (
                    <TableCell>
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
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </VStack>
      ) : query.data ? (
        <Empty title="No repositories assigned">
          {maintain
            ? "Request a repository above. An administrator can then verify access and approve it."
            : "A team maintainer can request repository access."}
        </Empty>
      ) : null}
      {after || query.data?.next_after_id ? (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"First page"}
            variant="secondary"
            type="submit"

            isDisabled={!after}
            onClick={() => setAfter(0)}
          />
          <Button
            label={"Next"}
            variant="secondary"
            type="submit"

            isDisabled={!query.data?.next_after_id}
            onClick={() => setAfter(query.data?.next_after_id ?? 0)}
          />
        </HStack>
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
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <VStack gap={3}>
              <Heading level={1}>Repository requests</Heading>
              <Text as="p">
                {admin
                  ? "Platform administration · Verify GitHub access and assign repositories to teams."
                  : "Track repository requests for your teams."}
              </Text>
            </VStack>
            <Link to="/teams">Teams</Link>
          </HStack>
        </>
      ) : (
        <Heading level={2}>Repository requests</Heading>
      )}
      <HStack gap={3} wrap="wrap" vAlign="center">
        <Selector
          label={"Status"}
          options={[
            { value: "pending", label: "Pending" },
            { value: "approved", label: "Approved" },
            { value: "rejected", label: "Rejected" },
            { value: "withdrawn", label: "Withdrawn" },
            { value: "", label: "All requests" },
          ]}
          value={status}
          onChange={(value) => {
            setStatus(value);
            setBefore(null);
          }}
        />
        <Text>{query.data ? `${query.data.pending} pending` : ""}</Text>
      </HStack>
      <Freshness query={query} />
      {query.data?.items.length ? (
        <VStack gap={3}>
          {query.data.items.map((request) => (
            <VStack as="article" gap={3} key={request.id}>
              <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
                <VStack gap={3}>
                  <Heading level={3}>{request.repository_name}</Heading>
                  <Text color="secondary" display="block" type="supporting">
                    <Link
                      to={`/teams/${request.team_id}?team_id=${request.team_id}`}
                    >
                      {request.team_name}
                    </Link>{" "}
                    · Requested {time(request.submitted_at)}
                  </Text>
                </VStack>
                <Text>{request.status}</Text>
              </HStack>
              <Text as="p">{request.reason}</Text>
              {request.decision_reason ? (
                <Text as="p">
                  Decision: {request.decision_reason} ·{" "}
                  {time(request.decided_at)}
                </Text>
              ) : null}
              {request.status === "pending" ? (
                <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
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
                </HStack>
              ) : null}
            </VStack>
          ))}
        </VStack>
      ) : query.data ? (
        <Empty title="No requests in this view">
          {status === "pending"
            ? "There are no repository requests waiting for approval."
            : "Choose another status to see earlier requests."}
        </Empty>
      ) : null}
      {before || query.data?.next_before_id ? (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"Latest requests"}
            variant="secondary"
            type="submit"

            isDisabled={!before}
            onClick={() => setBefore(null)}
          />
          <Button
            label={"Older requests"}
            variant="secondary"
            type="submit"

            isDisabled={!query.data?.next_before_id}
            onClick={() => setBefore(query.data?.next_before_id ?? null)}
          />
        </HStack>
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
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      {admin ? (
        <>
          <Freshness query={connections} />
          <Selector
            label={"Assigned connection"}
            options={[
              !connections.data?.items.some(
                (connection) => connection.id === selected.id,
              )
                ? { value: String(selected.id), label: selected.name }
                : null,
              connections.data?.items.map((connection) => ({
                value: String(connection.id),
                label:
                  connection.name +
                  String(
                    connection.state !== "enabled"
                      ? " (paused or unavailable)"
                      : "",
                  ),
              })),
            ]
              .flat()
              .filter((option) => option != null)}
            value={String(selected.id)}
            onChange={(value) => {
              const next = connections.data?.items.find(
                (connection) => connection.id === Number(value),
              );
              if (next) {
                setSelected(next);
                setRoute(-1);
                setEffort("");
              }
            }}
          />
          {after > 0 || connections.data?.next_after_id ? (
            <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
              <Button
                label={"First connections"}
                variant="secondary"
                type="button"

                isDisabled={after === 0}
                onClick={() => setAfter(0)}
              />
              <Button
                label={"Next connections"}
                variant="secondary"
                type="button"

                isDisabled={!connections.data?.next_after_id}
                onClick={() => setAfter(connections.data?.next_after_id ?? 0)}
              />
            </HStack>
          ) : null}
        </>
      ) : null}
      <Grid gap={4} columns={{ minWidth: 240, max: 4, repeat: "fit" }}>
        <Selector
          label={"Model"}
          options={[
            { value: String(-1), label: "Inherit deployment defaults" },
            selected.allowed_routes.map((choice, index) => ({
              value: String(index),
              label:
                String(
                  choice.provider === "openai-codex"
                    ? "OpenAI Codex"
                    : "Anthropic",
                ) +
                " " +
                "·" +
                choice.model,
            })),
          ]
            .flat()
            .filter((option) => option != null)}
          value={String(route)}
          onChange={(value) => {
            const index = Number(value);
            setRoute(index);
            setEffort(
              selected.allowed_routes[index]?.reasoning_efforts[0] ?? "",
            );
          }}
        />
        {choice ? (
          <Selector
            label={"Reasoning"}
            options={[
              choice.reasoning_efforts.map((effort) => ({
                value: effort,
                label: effort,
              })),
            ]
              .flat()
              .filter((option) => option != null)}
            isRequired={true}
            value={effort}
            onChange={(value) => setEffort(value)}
          />
        ) : null}
      </Grid>
      {!selected.allowed_routes.length ? (
        <Text as="p" color="secondary">
          This connection currently allows deployment defaults only. An
          administrator can add model choices.
        </Text>
      ) : null}
      {admin ? (
        <NumberInput
          isIntegerOnly
          label={"Maximum concurrent team reviews"}
          isRequired={true}
          min={1}
          max={2147483647}
          step={1}
          value={maxConcurrency}
          onChange={(value) => setMaxConcurrency(value)}
          {...({
            required: true,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />
      ) : null}

      <TextArea
        label={"Reason"}
        isRequired={true}
        maxLength={500}
        value={reason}
        onChange={(value) => setReason(value.slice(0, 500))}
        {...({
          required: true,
        } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
      />

      <Text as="p" color="secondary">
        Model changes apply to newly admitted reviews. Queued and running
        reviews keep their original account and model. Capacity limits apply to
        new claims across all workers and connections; reviews already claimed
        can finish.
      </Text>
      {save.isError ? (
        <Text as="p" role="alert">
          {save.error.message}
        </Text>
      ) : null}
      {save.isSuccess ? (
        <Text as="p" role="status">
          Team model policy saved.
        </Text>
      ) : null}
      <Button
        label={String(save.isPending ? "Saving…" : "Save model policy")}
        variant="primary"
        type="submit"
        isDisabled={save.isPending}
      />
    </Form>
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
    <VStack gap={4} as="section">
      <Heading level={2}>Model and account</Heading>
      <Freshness query={query} />
      {policy ? (
        <>
          <VStack as="dl" gap={2}>
            <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
              <dt>
                <Text color="secondary">Connection</Text>
              </dt>
              <dd>
                <Link
                  to={`/model-connections/${policy.connection.id}?team_id=${team.id}`}
                >
                  {policy.connection.name}
                </Link>{" "}
                · {policy.connection.team_id ? "Dedicated" : "Shared"}
              </dd>
            </HStack>
            <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
              <dt>
                <Text color="secondary">Model</Text>
              </dt>
              <dd>
                {policy.effective_provider === "openai-codex"
                  ? "OpenAI Codex"
                  : "Anthropic"}{" "}
                · {policy.effective_model}
              </dd>
            </HStack>
            <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
              <dt>
                <Text color="secondary">Reasoning</Text>
              </dt>
              <dd>{policy.effective_reasoning_effort}</dd>
            </HStack>
            <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
              <dt>
                <Text color="secondary">Team capacity</Text>
              </dt>
              <dd>
                {policy.max_concurrency} concurrent reviews across connections
              </dd>
            </HStack>
            <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
              <dt>
                <Text color="secondary">Connection capacity</Text>
              </dt>
              <dd>
                {policy.connection.max_concurrency} concurrent reviews shared by
                its teams
              </dd>
            </HStack>
            <HStack gap={3} wrap="wrap" hAlign="between" vAlign="start">
              <dt>
                <Text color="secondary">Source</Text>
              </dt>
              <dd>
                {policy.provider === null
                  ? "Inherited from deployment defaults"
                  : "Team model policy"}
              </dd>
            </HStack>
          </VStack>
          {policy.connection.state !== "enabled" ? (
            <Text as="p">
              This connection is paused or needs attention. Open the connection
              to check its status.
            </Text>
          ) : null}
          {maintain ? (
            <Collapsible
              defaultIsOpen={false}
              trigger={
                <HStack gap={3} wrap="wrap" vAlign="center">
                  Change the team's model policy
                </HStack>
              }
            >
              <VStack gap={4}>
                <TeamModelEditor
                  key={`${policy.revision}:${policy.connection.revision}`}
                  policy={policy}
                />
              </VStack>
            </Collapsible>
          ) : null}
        </>
      ) : null}
    </VStack>
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
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <VStack gap={3}>
              <Heading level={1}>{team.name}</Heading>
              <Text as="p">
                {team.description ||
                  "Team repositories, membership, and access history."}
              </Text>
            </VStack>
            <Link to={`/?team_id=${team.id}`}>View team activity</Link>
          </HStack>
          <TabList
            aria-label="Team views"
            value={tab}
            onChange={(value) => {
              const next = new URLSearchParams(params);
              next.set("tab", value);
              next.set("team_id", String(team.id));
              setParams(next);
            }}
            hasDivider
          >
            <Tab value="repositories" label="Repositories" />
            <Tab value="members" label="Members" />
            <Tab value="models" label="Model and account" />
            <Tab
              value="requests"
              label={`Requests${team.pending_requests ? ` (${team.pending_requests})` : ""}`}
            />
            {isAdmin(scope.current.role) ? (
              <Tab value="audit" label="Audit log" />
            ) : null}
          </TabList>
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
            <Collapsible
              defaultIsOpen={false}
              trigger={
                <HStack gap={3} wrap="wrap" vAlign="center">
                  Edit team details
                </HStack>
              }
            >
              <VStack gap={4}>
                <TeamEditor key={team.revision} team={team} />
              </VStack>
            </Collapsible>
          ) : null}
        </>
      ) : null}
    </>
  );
}
