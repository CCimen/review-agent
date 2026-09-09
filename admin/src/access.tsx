import { Button } from "@astryxdesign/core/Button";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
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
import { TextInput } from "@astryxdesign/core/TextInput";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { InputHTMLAttributes } from "react";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import type { Account } from "./api";
import { read, write } from "./api";
import type { components } from "./api.generated";
import { ScopedAnchor, isAdmin } from "./scope";
import { Form, Freshness, time } from "./ui";

export function RepositoryTabs({ role }: { role: Account["role"] }) {
  const { pathname } = useLocation();
  return (
    <TabList
      aria-label="Repository views"
      value={pathname}
      onChange={() => {}}
      hasDivider
    >
      <Tab
        value="/repositories"
        href="/repositories"
        as={ScopedAnchor}
        label="Activity"
      />
      {isAdmin(role) && (
        <Tab
          value="/access"
          href="/access"
          as={ScopedAnchor}
          label="Access management"
        />
      )}
    </TabList>
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
    queryFn: ({ signal }) =>
      read<Connection>("/api/access/connection", signal),
    refetchInterval: 60_000,
  });
  const data = query.data;
  return (
    <VStack gap={4} as="section">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <Heading level={2}>GitHub App</Heading>
        {data && <Text>{connectionLabels[data.status]}</Text>}
      </HStack>
      <VStack gap={4}>
        <Freshness query={query} interval={60} />
        {data?.app && (
          <Text as="p">
            <strong>{data.app}</strong> · {data.owner}
          </Text>
        )}
        {data?.status === "connected" && (
          <Text as="p" color="secondary">
            GitHub API authentication and required App permissions verified.
            Installation access and webhook delivery are checked separately.
          </Text>
        )}
        {data?.status === "not_configured" && (
          <Text as="p">
            Configure the App ID and private key in the admin service’s
            deployment configuration to connect GitHub.
          </Text>
        )}
        {!!data?.issues?.length && (
          <VStack as="ul" gap={3}>
            {data.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </VStack>
        )}
        <HStack gap={3} wrap="wrap" vAlign="center">
          <Button
            label={"Check connection now"}
            variant="secondary"
            type="button"
            isDisabled={query.isFetching}
            onClick={() => void query.refetch()}
          />
          {data?.edit_url && (
            <AstryxLink
              href={data.edit_url}
              target="_blank"
              rel="noreferrer"
            >
              Edit GitHub App
              <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
            </AstryxLink>
          )}
          {data?.install_url && (
            <AstryxLink
              href={data.install_url}
              target="_blank"
              rel="noreferrer"
            >
              Install or manage access
              <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
            </AstryxLink>
          )}
          {data?.app_url && (
            <AstryxLink
              href={data.app_url}
              target="_blank"
              rel="noreferrer"
            >
              App page<VisuallyHidden> (opens in a new tab)</VisuallyHidden>
            </AstryxLink>
          )}
        </HStack>
        {data?.edit_url && (
          <Text as="p" color="secondary">
            Editing the App requires access to its owning GitHub account.
            Permission changes may also need approval in each installation.
          </Text>
        )}
      </VStack>
    </VStack>
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
  const [policy, setPolicy] = useState<"explicit" | "automatic">(
    installation.repository_activation,
  );
  const [checkEnabled, setCheckEnabled] = useState(false);
  const check = useQuery({
    queryKey: [
      "access",
      "installation-status",
      installation.installation_id,
    ],
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
        { policy, reason: "Installation activation policy updated" },
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });
  const sync = useMutation({
    mutationFn: () =>
      write(
        `/api/access/installations/${installation.installation_id}/sync`,
        "POST",
        { reason: "Repository inventory synchronized" },
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });
  const pending = approve.isPending || sync.isPending;
  const error = approve.error ?? sync.error;

  return (
    <VStack gap={4} as="article">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={2}>{installation.account}</Heading>
          <Text>Installation {installation.installation_id}</Text>
        </VStack>
        <Text>{installation.status}</Text>
      </HStack>
      <VStack gap={4}>
        <VStack as="dl" gap={2}>
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <dt>
              <Text color="secondary">GitHub grants access to</Text>
            </dt>
            <dd>
              {installation.repository_selection === "all"
                ? "All repositories"
                : "Selected repositories"}
            </dd>
          </HStack>
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <dt>
              <Text color="secondary">Review Agent accepts</Text>
            </dt>
            <dd>
              {installation.repository_activation === "automatic"
                ? "All accessible repositories, except manually disabled ones"
                : "Only explicitly enabled repositories"}
            </dd>
          </HStack>
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <dt>
              <Text color="secondary">Permissions</Text>
            </dt>
            <dd>
              contents {installation.contents_permission}, issues{" "}
              {installation.issues_permission}, pull requests{" "}
              {installation.pull_requests_permission}
            </dd>
          </HStack>
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <dt>
              <Text color="secondary">Stored state updated</Text>
            </dt>
            <dd>{dateTime.format(new Date(installation.updated_at))}</dd>
          </HStack>
        </VStack>
        <VStack gap={3}>
          <Button
            label={String(
              check.isFetching
                ? "Checking GitHub…"
                : "Check live GitHub status",
            )}
            variant="secondary"
            type="button"
            isDisabled={!configured || check.isFetching}
            onClick={() => {
              if (checkEnabled) void check.refetch();
              else setCheckEnabled(true);
            }}
          />
          {checkEnabled && <Freshness query={check} quiet />}
          {check.data && (
            <>
              <Text as="p">
                GitHub reports <strong>{check.data.status}</strong> ·{" "}
                {check.data.repository_selection === "all"
                  ? "All repositories"
                  : "Selected repositories"}
                . Checked {time(check.data.checked_at)}.
              </Text>
              {!!check.data.issues.length && (
                <VStack gap={3}>
                  <strong>Installation permissions need an update</strong>
                  <VStack as="ul" gap={3}>
                    {check.data.issues.map((issue) => (
                      <li key={issue}>{issue}</li>
                    ))}
                  </VStack>
                </VStack>
              )}
              {check.data.settings_url && (
                <AstryxLink
                  href={check.data.settings_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Edit installation on GitHub
                  <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
                </AstryxLink>
              )}
              {(check.data.status !== installation.status ||
                check.data.repository_selection !==
                  installation.repository_selection) && (
                <Text as="p" color="secondary">
                  Stored access differs from GitHub. Save the activation
                  policy below to refresh the installation state.
                </Text>
              )}
            </>
          )}
        </VStack>
        <Collapsible
          defaultIsOpen={false}
          trigger={
            <HStack gap={3} wrap="wrap" vAlign="center">
              Change Review Agent access
            </HStack>
          }
        >
          <VStack gap={4}>
            <Text as="p" color="secondary">
              Allow all accessible repositories to activate on their first
              review request, or require an administrator to enable each
              one. Switching to explicit enablement disables repositories
              activated automatically; manually enabled repositories remain
              enabled.
            </Text>
            <Form
              onSubmit={(event) => {
                event.preventDefault();
                approve.mutate();
              }}
            >
              <Selector
                label={"Repositories accepted by Review Agent"}
                options={[
                  {
                    value: "explicit",
                    label: "Only explicitly enabled repositories",
                  },
                  {
                    value: "automatic",
                    label: "All accessible repositories",
                  },
                ]}
                value={policy}
                onChange={(value) =>
                  setPolicy(
                    value === "automatic" ? "automatic" : "explicit",
                  )
                }
                isDisabled={pending || !configured}
              />

              {error && (
                <Text as="p" role="alert">
                  {error.message}
                </Text>
              )}
              <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
                <Button
                  label={String(
                    approve.isPending
                      ? "Saving…"
                      : "Save activation policy",
                  )}
                  variant="primary"
                  isDisabled={pending || !configured}
                  type="submit"
                />
                <Button
                  label={String(
                    sync.isPending ? "Syncing…" : "Sync repositories",
                  )}
                  variant="primary"
                  isDisabled={
                    pending ||
                    !configured ||
                    installation.repository_selection !== "selected"
                  }
                  type="button"
                  onClick={() => sync.mutate()}
                />
              </HStack>
              {installation.repository_selection === "all" && (
                <Text as="p" color="secondary">
                  All-repository installations discover repositories on the
                  first review request. Use Add repository below to enable a
                  specific repository now.
                </Text>
              )}
              {(approve.isSuccess || sync.isSuccess) && (
                <Text as="p" role="status">
                  {approve.isSuccess
                    ? "Activation policy saved."
                    : "Repository inventory refreshed."}
                </Text>
              )}
            </Form>
          </VStack>
        </Collapsible>
      </VStack>
    </VStack>
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
  const add = useMutation({
    mutationFn: () =>
      write<RepositoryAccess>("/api/access/repositories/onboard", "POST", {
        repository: repository.trim(),
        profile: profile.trim(),
        reason: "Repository enabled for reviews",
      }),
    onSuccess: async () => {
      setRepository("");
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });
  return (
    <Collapsible
      defaultIsOpen={false}
      trigger={
        <HStack gap={3} wrap="wrap" vAlign="center">
          Add repository
        </HStack>
      }
    >
      <VStack gap={4}>
        <VStack gap={4}>
          <Text as="p">
            Enable reviews for one repository the GitHub App can access.
            Works with both all-repository and selected-repository
            installations.
          </Text>
          <Text as="p" color="secondary">
            For a selected-repository installation, add the repository in
            GitHub first. Disabling a repository here retains its review
            history.
          </Text>
          {!configured && (
            <Text as="p">
              Connect the GitHub App credentials before adding repositories.
            </Text>
          )}
          <Form
            onSubmit={(event) => {
              event.preventDefault();
              add.mutate();
            }}
          >
            <fieldset disabled={!configured || add.isPending}>
              <VStack gap={4}>
                <TextInput
                  label={"Repository"}
                  isRequired={true}
                  placeholder="owner/repository"
                  value={repository}
                  onChange={(value) => setRepository(value)}
                  {...({
                    required: true,
                    maxLength: 260,
                  } satisfies InputHTMLAttributes<HTMLInputElement>)}
                />

                <TextInput
                  label={"Review profile"}
                  isRequired={true}
                  value={profile}
                  onChange={(value) => setProfile(value)}
                  {...({
                    required: true,
                    maxLength: 100,
                  } satisfies InputHTMLAttributes<HTMLInputElement>)}
                />
              </VStack>
            </fieldset>
            {add.error && (
              <Text as="p" role="alert">
                {add.error.message} Check that the GitHub installation
                includes this repository and has the required permissions.
              </Text>
            )}
            {add.isSuccess && (
              <Text as="p" role="status">
                Reviews enabled for {add.data.repository}.
              </Text>
            )}
            <Button
              label={String(
                add.isPending
                  ? "Verifying repository…"
                  : "Verify and enable reviews",
              )}
              variant="primary"
              type="submit"
              isDisabled={
                !configured ||
                add.isPending ||
                !repository.trim() ||
                !profile.trim()
              }
            />
          </Form>
        </VStack>
      </VStack>
    </Collapsible>
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
  const [profile, setProfile] = useState(
    repository.profile ?? defaultProfile,
  );
  const mutation = useMutation({
    mutationFn: () =>
      write<RepositoryAccess>(
        `/api/access/repositories/${repository.repository_id}/${repository.enabled ? "disable" : "enable"}`,
        "POST",
        repository.enabled
          ? { reason: "Repository reviews disabled" }
          : { profile, reason: "Repository reviews enabled" },
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["access"] });
    },
  });

  return (
    <TableRow>
      <TableHeaderCell scope="row">
        <strong>{repository.repository}</strong>
        <Text color="secondary" display="block" type="supporting">
          ID {repository.repository_id}
        </Text>
      </TableHeaderCell>
      <TableCell>{repository.access.replaceAll("_", " ")}</TableCell>
      <TableCell>
        {repository.enabled ? "Enabled" : "Disabled"}
        {repository.automatic_activation_blocked && (
          <Text color="secondary" display="block" type="supporting">
            Automatic activation blocked
          </Text>
        )}
      </TableCell>
      <TableCell>{repository.profile ?? "—"}</TableCell>
      <TableCell>
        {!repository.enabled && repository.access !== "available" ? (
          <Text color="secondary">
            Restore GitHub access before enabling
          </Text>
        ) : (
          <Collapsible
            defaultIsOpen={false}
            trigger={
              <HStack gap={3} wrap="wrap" vAlign="center">
                {repository.enabled
                  ? "Disable reviews…"
                  : "Enable reviews…"}
              </HStack>
            }
          >
            <VStack gap={4}>
              <Form
                onSubmit={(event) => {
                  event.preventDefault();
                  mutation.mutate();
                }}
              >
                <Text as="p" color="secondary">
                  {repository.enabled
                    ? "Disabling stops new review requests and blocks automatic activation. Review history is retained. To revoke GitHub access too, remove the repository in the GitHub App installation settings."
                    : "Enabling admits new review requests using the selected profile."}
                </Text>
                {!repository.enabled && (
                  <TextInput
                    label={"Profile"}
                    isRequired={true}
                    isDisabled={mutation.isPending}
                    value={profile}
                    onChange={(value) => setProfile(value)}
                    {...({
                      required: true,
                      maxLength: 100,
                    } satisfies InputHTMLAttributes<HTMLInputElement>)}
                  />
                )}

                {mutation.isError && (
                  <Text as="p" role="alert">
                    {mutation.error.message}
                  </Text>
                )}
                <Button
                  label={String(
                    mutation.isPending
                      ? "Saving…"
                      : repository.enabled
                        ? "Confirm disable"
                        : "Confirm enable",
                  )}
                  variant="primary"
                  type="submit"
                  isDisabled={mutation.isPending}
                />
              </Form>
            </VStack>
          </Collapsible>
        )}
      </TableCell>
    </TableRow>
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
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Repositories &amp; access</Heading>
          <Text as="p">
            Approve GitHub App installations and control review access for
            each repository.
          </Text>
        </VStack>
      </HStack>
      <RepositoryTabs role="admin" />
      <GitHubConnection />
      {capability && !capability.configured && (
        <Text as="p" role="status">
          {capability.detail}
        </Text>
      )}
      {(installations.isPending || repositories.isPending) && (
        <Text as="p" role="status">
          Loading repository access…
        </Text>
      )}
      {failed && (
        <VStack gap={3} role="alert">
          <Text as="p">Could not load repository access.</Text>
          <Button
            label={"Retry"}
            variant="primary"
            type="submit"
            onClick={() => {
              void installations.refetch();
              void repositories.refetch();
            }}
          />
        </VStack>
      )}
      {installations.data && (
        <VStack gap={4} as="section">
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <Heading level={2}>Installations</Heading>
            <Text>{installations.data.items.length} shown</Text>
          </HStack>
          {installations.data.items.length ? (
            <VStack gap={3}>
              {installations.data.items.map((installation) => (
                <InstallationPanel
                  key={installation.installation_id}
                  installation={installation}
                  configured={installations.data.capability.configured}
                />
              ))}
            </VStack>
          ) : (
            <Text as="p">No GitHub App installations found.</Text>
          )}
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <Button
              label={"Previous installations"}
              variant="primary"
              type="button"
              isDisabled={installationCursors.length === 1}
              onClick={() =>
                setInstallationCursors((current) => current.slice(0, -1))
              }
            />
            <Button
              label={"Next installations"}
              variant="primary"
              type="button"
              isDisabled={installations.data.next_after_id === null}
              onClick={() => {
                const next = installations.data?.next_after_id;
                if (next !== null && next !== undefined)
                  setInstallationCursors((current) => [...current, next]);
              }}
            />
          </HStack>
        </VStack>
      )}
      {capability && (
        <AddRepository
          configured={capability.configured}
          defaultProfile={capability.default_profile}
        />
      )}
      {repositories.data && (
        <VStack gap={4} as="section">
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <Heading level={2}>Repositories</Heading>
            <Text>{repositories.data.items.length} shown</Text>
          </HStack>
          {repositories.data.items.length ? (
            <VStack gap={4}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHeaderCell scope="col">
                      Repository
                    </TableHeaderCell>
                    <TableHeaderCell scope="col">
                      Provider access
                    </TableHeaderCell>
                    <TableHeaderCell scope="col">Reviews</TableHeaderCell>
                    <TableHeaderCell scope="col">Profile</TableHeaderCell>
                    <TableHeaderCell scope="col">Action</TableHeaderCell>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {repositories.data.items.map((repository) => (
                    <RepositoryRow
                      key={repository.repository_id}
                      repository={repository}
                      defaultProfile={
                        repositories.data.capability.default_profile
                      }
                    />
                  ))}
                </TableBody>
              </Table>
            </VStack>
          ) : (
            <Text as="p">No repositories found.</Text>
          )}
          <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
            <Button
              label={"Previous repositories"}
              variant="primary"
              type="button"
              isDisabled={repositoryCursors.length === 1}
              onClick={() =>
                setRepositoryCursors((current) => current.slice(0, -1))
              }
            />
            <Button
              label={"Next repositories"}
              variant="primary"
              type="button"
              isDisabled={repositories.data.next_after_id === null}
              onClick={() => {
                const next = repositories.data?.next_after_id;
                if (next !== null && next !== undefined)
                  setRepositoryCursors((current) => [...current, next]);
              }}
            />
          </HStack>
        </VStack>
      )}
    </>
  );
}
