import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Divider } from "@astryxdesign/core/Divider";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { Section } from "@astryxdesign/core/Section";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { InputHTMLAttributes } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Form } from "./ui";
// Login layout adapted from Astryx's Login Card template.
// Copyright (c) Meta Platforms, Inc. and affiliates.
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Center } from "@astryxdesign/core/Center";
import { VStack } from "@astryxdesign/core/Layout";
import { Heading, Text } from "@astryxdesign/core/Text";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { GitPullRequest } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type {
  Account,
  AccountPage,
  AccountUpdate,
  NewAccount,
  PasswordChange,
  IdentityProvider,
  AccountIdentity,
  OIDCStart,
} from "./api";
import { login, read, write } from "./api";
import { isAdmin, roleLabels, ScopedAnchor, useScope } from "./scope";
import { Stat } from "./ui";
import { RegistrationAccess } from "./registration";
import type { components } from "./api.generated";

export function SettingsTabs() {
  const { current } = useScope();
  const { pathname } = useLocation();
  return (
    <TabList
      aria-label="Settings views"
      value={pathname}
      onChange={() => {}}
      hasDivider
    >
      {current.role === "owner" ? (
        <Tab
          value="/settings"
          href="/settings"
          as={ScopedAnchor}
          label="General"
        />
      ) : null}
      <Tab
        value="/users"
        href="/users"
        as={ScopedAnchor}
        label="Users & roles"
      />
      <Tab
        value="/integrations"
        href="/integrations"
        as={ScopedAnchor}
        label="Integrations"
      />
    </TabList>
  );
}

export function Login() {
  const client = useQueryClient();
  const { search, hash, pathname } = useLocation();
  const navigate = useNavigate();
  const [token, setToken] = useState(
    () => new URLSearchParams(hash.slice(1)).get("register_token") ?? "",
  );
  const [mode, setMode] = useState<"login" | "register" | "complete">(() =>
    token
      ? "complete"
      : new URLSearchParams(search).get("register") === "1"
        ? "register"
        : "login",
  );
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [sent, setSent] = useState(false);
  const registration = useQuery({
    queryKey: ["registration-availability"],
    queryFn: ({ signal }) =>
      read<components["schemas"]["RegistrationAvailability"]>(
        "/api/auth/registration",
        signal,
      ),
  });
  const provider = useQuery({
    queryKey: ["identity-provider"],
    queryFn: ({ signal }) =>
      read<IdentityProvider>("/api/auth/oidc/provider", signal),
  });
  const sso = useMutation({
    mutationFn: () =>
      write<OIDCStart>(
        mode === "register"
          ? "/api/auth/oidc/register"
          : "/api/auth/oidc/start",
        "POST",
      ),
    onSuccess: ({ authorization_url }) =>
      window.location.assign(authorization_url),
  });
  const ssoError = new URLSearchParams(search).get("sso_error");
  const ssoMessage =
    ssoError === "account"
      ? "Ask your administrator to provision your account, or sign in with your password and link organization sign-in from Your account."
      : ssoError === "registration"
        ? "Registration is not allowed for this organization account. Use an approved email, or sign in to your existing account and link organization sign-in."
        : ssoError === "expired"
          ? "This sign-in request expired or was already used. Start again from this page."
          : ssoError === "cancelled"
            ? "Organization sign-in was cancelled. You can try again."
            : ssoError
              ? "Organization sign-in could not be verified. Try again or contact your administrator."
              : null;
  const mutation = useMutation({
    mutationFn: () =>
      mode === "register"
        ? write("/api/auth/register", "POST", { email })
        : mode === "complete"
          ? write("/api/auth/register/complete", "POST", {
              token,
              password,
            })
          : login(email, password),
    onSuccess: async () => {
      if (mode === "register") {
        setSent(true);
        return;
      }
      setPassword("");
      setToken("");
      setMode("login");
      client.removeQueries({
        predicate: (query) => query.queryKey[0] !== "me",
      });
      await client.resetQueries({ queryKey: ["me"] });
    },
  });
  useEffect(() => {
    document.title =
      mode === "login"
        ? "Review Agent · Sign in"
        : "Review Agent · Register";
  }, [mode]);
  useEffect(() => {
    if (token && hash)
      void navigate({ pathname, search, hash: "" }, { replace: true });
  }, [token, hash, pathname, search, navigate]);
  const switchMode = (next: "login" | "register") => {
    setMode(next);
    setToken("");
    setPassword("");
    setSent(false);
    mutation.reset();
    sso.reset();
  };
  const pending = mutation.isPending || sso.isPending;
  const canRegister =
    registration.data?.enabled || provider.data?.registration_enabled;
  const showForm = mode !== "register" || registration.data?.enabled;
  const title =
    mode === "complete"
      ? "Choose your password"
      : mode === "register"
        ? "Create your account"
        : "Sign in";
  return (
    <main>
      <Center minHeight="100dvh" padding={6}>
        <VStack gap={6} width="100%" maxWidth={400} hAlign="stretch">
          <VStack gap={2} hAlign="center">
            <GitPullRequest size={28} aria-hidden="true" />
            <Text weight="bold" size="lg">
              Review Agent
            </Text>
          </VStack>
          <Card padding={8} width="100%">
            <VStack gap={5}>
              <VStack gap={2}>
                <Heading level={1}>{title}</Heading>
                <Text color="secondary">
                  {mode === "complete"
                    ? "Finish registration with a password of 15–128 characters."
                    : mode === "register"
                      ? "Use an allowed email address. We’ll send a link to verify it and choose your password."
                      : "Review activity and administration for your team."}
                </Text>
              </VStack>
              {showForm && (
                <Form
                  onSubmit={(event) => {
                    event.preventDefault();
                    mutation.mutate();
                  }}
                >
                  {mode !== "complete" && (
                    <TextInput
                      label="Email address"
                      id="login-email"
                      type="email"
                      hasAutoFocus
                      isRequired
                      isDisabled={pending}
                      value={email}
                      onChange={(value) => {
                        setEmail(value);
                        setSent(false);
                      }}
                      {...({
                        autoComplete: "username",
                        required: true,
                        maxLength: 320,
                      } satisfies InputHTMLAttributes<HTMLInputElement>)}
                    />
                  )}
                  {mode !== "register" && (
                    <TextInput
                      label={
                        mode === "complete" ? "New password" : "Password"
                      }
                      id="login-password"
                      type="password"
                      isRequired
                      hasAutoFocus={mode === "complete"}
                      isDisabled={pending}
                      value={password}
                      onChange={setPassword}
                      {...({
                        autoComplete:
                          mode === "complete"
                            ? "new-password"
                            : "current-password",
                        required: true,
                        minLength: mode === "complete" ? 15 : undefined,
                        maxLength: 128,
                      } satisfies InputHTMLAttributes<HTMLInputElement>)}
                    />
                  )}
                  {mutation.isError && (
                    <Banner
                      status="error"
                      title={
                        mode === "login"
                          ? "Could not sign in"
                          : "Could not complete registration"
                      }
                      description={mutation.error.message}
                    />
                  )}
                  {sent && (
                    <Text role="status">
                      If this email is allowed and has no account, a
                      verification link is on its way. Check your inbox and
                      spam folder. The link expires in 30 minutes. Wait one
                      minute before requesting another.
                    </Text>
                  )}
                  <Button
                    label={
                      mode === "complete"
                        ? "Create account and sign in"
                        : mode === "register"
                          ? sent
                            ? "Send another link"
                            : "Send verification link"
                          : "Sign in"
                    }
                    type="submit"
                    variant="primary"
                    isLoading={mutation.isPending}
                    isDisabled={pending}
                    width="100%"
                  />
                </Form>
              )}
              {mode === "register" &&
                registration.data &&
                provider.data &&
                !canRegister && (
                  <Text role="status">
                    Registration is currently unavailable. Contact your
                    administrator for an account.
                  </Text>
                )}
              {ssoMessage && (
                <Banner
                  status="error"
                  title="Could not complete organization sign-in"
                  description={ssoMessage}
                />
              )}
              {mode !== "complete" &&
                provider.data?.name &&
                (mode === "login" ||
                  provider.data.registration_enabled) && (
                  <VStack gap={4}>
                    {showForm && (
                      <Divider
                        label={
                          mode === "register"
                            ? "Or register with"
                            : "Or sign in with"
                        }
                      />
                    )}
                    <Button
                      label={`Continue with ${provider.data.name}`}
                      variant="secondary"
                      width="100%"
                      isLoading={sso.isPending}
                      isDisabled={pending}
                      onClick={() => sso.mutate()}
                    />
                    {sso.isError && (
                      <Banner
                        status="error"
                        title="Organization sign-in is unavailable"
                        description={sso.error.message}
                      />
                    )}
                  </VStack>
                )}
              {provider.isError && (
                <VStack gap={2}>
                  <Text role="alert" color="secondary">
                    Could not check organization sign-in.
                  </Text>
                  <Button
                    label="Check again"
                    variant="ghost"
                    isLoading={provider.isFetching}
                    onClick={() => void provider.refetch()}
                  />
                </VStack>
              )}
              {registration.isError && (
                <VStack gap={2}>
                  <Text role="alert" color="secondary">
                    Could not check whether registration is open.
                  </Text>
                  <Button
                    label="Check registration"
                    variant="ghost"
                    isLoading={registration.isFetching}
                    onClick={() => void registration.refetch()}
                  />
                </VStack>
              )}
              {mode !== "login" ? (
                <VStack gap={2}>
                  {mode === "complete" && (
                    <Button
                      label="Request a new registration link"
                      variant="ghost"
                      isDisabled={pending}
                      onClick={() => switchMode("register")}
                    />
                  )}
                  <Button
                    label="Back to sign in"
                    variant="ghost"
                    isDisabled={pending}
                    onClick={() => switchMode("login")}
                  />
                </VStack>
              ) : canRegister ? (
                <VStack gap={2} hAlign="center">
                  <Text type="supporting">Don’t have an account?</Text>
                  <Button
                    label="Register"
                    variant="ghost"
                    isDisabled={pending}
                    onClick={() => switchMode("register")}
                  />
                </VStack>
              ) : null}
            </VStack>
          </Card>
          <Text type="supporting">
            Ask your Review Agent administrator for an account or a password
            reset.
          </Text>
        </VStack>
      </Center>
    </main>
  );
}

export function Users({ current }: { current: Account }) {
  const client = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [adding, setAdding] = useState(false);
  const addButton = useRef<HTMLButtonElement>(null);
  const [createdEmail, setCreatedEmail] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<NewAccount["role"]>("member");
  const [filter, setFilter] = useState("");
  const [view, setView] = useState("all");
  const query = useQuery({
    queryKey: ["users", offset],
    queryFn: ({ signal }) =>
      read<AccountPage>(`/api/users?offset=${offset}`, signal),
  });
  const create = useMutation({
    mutationFn: () =>
      write<Account>("/api/users", "POST", {
        email,
        password,
        role,
        reason: "Account created",
      } satisfies NewAccount),
    onSuccess: async (account) => {
      setEmail("");
      setPassword("");
      setRole("member");
      setAdding(false);
      setCreatedEmail(account.email);
      await client.invalidateQueries({ queryKey: ["users"] });
    },
  });
  const page = query.data?.items ?? [];
  const needle = filter.trim().toLowerCase();
  const visible = page.filter(
    (account) =>
      account.email.toLowerCase().includes(needle) &&
      (view === "all" ||
        (view === "disabled" ? !account.active : account.role === view)),
  );
  useEffect(() => {
    document.title = "Review Agent · Users";
  }, []);
  useEffect(() => {
    if (createdEmail && !create.isPending) addButton.current?.focus();
  }, [createdEmail, create.isPending]);
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Users &amp; roles</Heading>
          <Text as="p">
            Platform administration · Manage accounts and platform roles.
            Team roles are assigned in Teams.
          </Text>
        </VStack>
        <Button
          label={String(adding ? "Cancel adding user" : "Add user")}
          variant="primary"
          type="submit"
          ref={addButton}
          aria-expanded={adding}
          aria-controls="add-user"
          isDisabled={create.isPending}
          onClick={() => {
            setAdding((value) => !value);
            setPassword("");
            setCreatedEmail("");
            create.reset();
          }}
        />
      </HStack>
      <SettingsTabs />
      {current.role === "owner" && (
        <Collapsible trigger="Registration allowlist" defaultIsOpen={false}>
          <RegistrationAccess />
        </Collapsible>
      )}
      <Collapsible
        defaultIsOpen={false}
        trigger={
          <HStack gap={3} wrap="wrap" vAlign="center">
            What the roles allow
          </HStack>
        }
      >
        <VStack gap={4}>
          <Text as="p">
            <strong>Members</strong> receive access through their teams as
            viewers or maintainers. <strong>Global viewers</strong> can read
            review data across the deployment. <strong>Admins</strong>{" "}
            manage teams, repositories, and member accounts.{" "}
            <strong>Owners</strong> also manage privileged accounts,
            provider credentials, and platform settings.
          </Text>
        </VStack>
      </Collapsible>
      {createdEmail && (
        <Text as="p" role="status">
          Account created for {createdEmail}.
        </Text>
      )}
      {adding && (
        <Section variant="transparent" padding={0} maxWidth={760}>
          <Form
            id="add-user"
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate();
            }}
          >
            <Heading level={2}>Add a user</Heading>
            <Grid
              gap={4}
              columns={{ minWidth: 240, max: 2, repeat: "fit" }}
            >
              <TextInput
                label={"Email address"}
                type="email"
                hasAutoFocus={true}
                isDisabled={create.isPending}
                isRequired={true}
                value={email}
                onChange={(value) => setEmail(value)}
                {...({
                  autoComplete: "off",
                  required: true,
                  maxLength: 320,
                } satisfies InputHTMLAttributes<HTMLInputElement>)}
              />

              <VStack gap={2}>
                <TextInput
                  label={"Initial password"}
                  type="password"
                  isDisabled={create.isPending}
                  isRequired={true}
                  value={password}
                  onChange={(value) => setPassword(value)}
                  {...({
                    autoComplete: "new-password",
                    required: true,
                    minLength: 15,
                    maxLength: 128,
                  } satisfies InputHTMLAttributes<HTMLInputElement>)}
                />
                <Text color="secondary">
                  15–128 characters. Share it privately with the user.
                </Text>
              </VStack>
              <Selector
                label={"Role"}
                options={[
                  Object.entries(roleLabels)
                    .filter(
                      ([value]) =>
                        current.role === "owner" ||
                        value === "member" ||
                        value === "viewer",
                    )
                    .map(([value, label]) => ({
                      value: value,
                      label: label,
                    })),
                ]
                  .flat()
                  .filter((option) => option != null)}
                value={role}
                onChange={(value) => setRole(value as Account["role"])}
                isDisabled={create.isPending}
              />
            </Grid>

            {create.isError && (
              <Text as="p" role="alert">
                {create.error.message}
              </Text>
            )}
            <HStack>
              <Button
                label={String(
                  create.isPending ? "Adding user…" : "Add user",
                )}
                variant="primary"
                type="submit"
                isDisabled={create.isPending}
              />
            </HStack>
          </Form>
        </Section>
      )}
      {query.isPending && (
        <Text as="p" role="status">
          Loading users…
        </Text>
      )}
      {query.isError && (
        <VStack gap={3} role="alert">
          <Text as="p">Could not load users.</Text>
          <Button
            label={"Retry"}
            variant="primary"
            type="submit"
            onClick={() => void query.refetch()}
          />
        </VStack>
      )}
      {query.data && (
        <>
          <Grid gap={4} columns={{ minWidth: 160, max: 6, repeat: "fit" }}>
            <Stat label="Accounts" value={query.data.total} />
            <Stat label="Admins" value={query.data.admin_count} />
            <Stat
              label="Team members & global viewers"
              value={query.data.total - query.data.admin_count}
            />
            <Stat label="Disabled" value={query.data.disabled_count} />
          </Grid>
          <Text as="p" color="secondary">
            Totals cover all accounts. Filters below apply to this page.
          </Text>
          <HStack gap={3} wrap="wrap" vAlign="center">
            <TextInput
              label={"Find a user"}
              id="user-filter"
              placeholder="Filter by email address"
              value={filter}
              onChange={(value) => setFilter(value)}
              {...({
                maxLength: 320,
              } satisfies InputHTMLAttributes<HTMLInputElement>)}
            />

            <Selector
              label={"Show"}
              options={[
                { value: "all", label: "All accounts" },
                { value: "owner", label: "Owners" },
                { value: "admin", label: "Admins" },
                { value: "member", label: "Members" },
                { value: "viewer", label: "Global viewers" },
                { value: "disabled", label: "Disabled" },
              ]}
              value={view}
              onChange={(value) => setView(value)}
            />
          </HStack>
          <Text as="p" role="status">
            Showing {visible.length} of {page.length} account
            {page.length === 1 ? "" : "s"} on this page.
          </Text>
          {visible.length ? (
            <VStack gap={4}>
              {visible.map((account) => (
                <UserRow
                  key={account.id}
                  account={account}
                  current={current}
                />
              ))}
            </VStack>
          ) : (
            <VStack gap={3}>
              <Heading level={2}>No matching accounts</Heading>
              <Text as="p">Clear the filter or change the role shown.</Text>
            </VStack>
          )}
        </>
      )}
      {(offset > 0 || query.data?.has_more) && (
        <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
          <Button
            label={"Previous"}
            variant="secondary"
            type="submit"
            isDisabled={offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - 50))}
          />
          <Text>Page {Math.floor(offset / 50) + 1}</Text>
          <Button
            label={"Next"}
            variant="secondary"
            type="submit"
            isDisabled={!query.data?.has_more || offset >= 10000}
            onClick={() => setOffset((value) => value + 50)}
          />
        </HStack>
      )}
    </>
  );
}

function UserRow({
  account,
  current,
}: {
  account: Account;
  current: Account;
}) {
  const client = useQueryClient();
  const protectedAccount =
    current.role !== "owner" && isAdmin(account.role);
  const [role, setRole] = useState(account.role);
  const [active, setActive] = useState(account.active);
  const [password, setPassword] = useState("");
  const [armed, setArmed] = useState(false);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const changed =
    role !== account.role || active !== account.active || password !== "";
  // Every field on this form is consequential, so a change is always reviewed
  // before it is applied. The list names what will happen, in the operator's
  // words rather than as a diff.
  const changes: string[] = [];
  if (role !== account.role)
    changes.push(
      `Change the role from ${roleLabels[account.role]} to ${roleLabels[role]}`,
    );
  if (active !== account.active)
    changes.push(active ? "Restore access" : "Disable access");
  if (password) changes.push("Replace the password");
  const self = account.id === current.id;
  const losesAdmin = self && isAdmin(account.role) && !isAdmin(role);
  const locksSelfOut = self && account.active && !active;
  // Editing the fields after arming invalidates what was reviewed.
  useEffect(() => {
    setArmed(false);
  }, [role, active, password]);
  useEffect(() => {
    if (armed) confirmButton.current?.focus();
  }, [armed]);
  const mutation = useMutation({
    mutationFn: () =>
      write<Account>(`/api/users/${account.id}`, "PATCH", {
        role,
        active,
        reason: "Account access updated",
        ...(password ? { password } : {}),
      } satisfies AccountUpdate),
    onSuccess: async () => {
      setPassword("");
      await client.invalidateQueries({ queryKey: ["users"] });
      if (account.id === current.id)
        await client.resetQueries({ queryKey: ["me"] });
    },
  });
  return (
    <Collapsible
      onOpenChange={(isOpen) => {
        if (isOpen && !mutation.isPending) {
          setRole(account.role);
          setActive(account.active);
          setPassword("");
          setArmed(false);
          mutation.reset();
        }
      }}
      defaultIsOpen={false}
      trigger={
        <HStack gap={3} wrap="wrap" vAlign="center">
          <Text>
            <strong>{account.email}</strong>
            {account.id === current.id && (
              <Text color="secondary" display="block" type="supporting">
                Your account
              </Text>
            )}
          </Text>
          <Text>{roleLabels[account.role]}</Text>
          <Text>{account.active ? "Active" : "Disabled"}</Text>
          <Text>{protectedAccount ? "View" : "Edit"}</Text>
        </HStack>
      }
    >
      <VStack gap={4} maxWidth={760}>
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            if (changed && !protectedAccount && !mutation.isPending)
              setArmed(true);
          }}
        >
          {protectedAccount ? (
            <Text as="p">
              Only a platform owner can change this account.
            </Text>
          ) : null}
          <fieldset disabled={protectedAccount || mutation.isPending}>
            <VStack gap={4}>
              <Grid
                gap={4}
                columns={{ minWidth: 240, max: 2, repeat: "fit" }}
              >
                <Selector
                  label={"Role"}
                  options={[
                    Object.entries(roleLabels)
                      .filter(
                        ([value]) =>
                          current.role === "owner" ||
                          value === "member" ||
                          value === "viewer" ||
                          value === account.role,
                      )
                      .map(([value, label]) => ({
                        value: value,
                        label: label,
                      })),
                  ]
                    .flat()
                    .filter((option) => option != null)}
                  value={role}
                  onChange={(value) => setRole(value as Account["role"])}
                  isDisabled={
                    mutation.isPending ||
                    protectedAccount ||
                    mutation.isPending
                  }
                />
                <Selector
                  label={"Access"}
                  options={[
                    { value: "true", label: "Active" },
                    { value: "false", label: "Disabled" },
                  ]}
                  value={String(active)}
                  onChange={(value) => setActive(value === "true")}
                  isDisabled={
                    mutation.isPending ||
                    protectedAccount ||
                    mutation.isPending
                  }
                />

                <TextInput
                  label={"Reset password"}
                  type="password"
                  isDisabled={mutation.isPending}
                  placeholder="Leave blank to keep it"
                  value={password}
                  onChange={(value) => setPassword(value)}
                  {...({
                    autoComplete: "new-password",
                    minLength: 15,
                    maxLength: 128,
                  } satisfies InputHTMLAttributes<HTMLInputElement>)}
                />
              </Grid>

              <Text as="p" color="secondary">
                Saving signs this account out on all devices. At least one
                active platform owner must remain, so the last one cannot be
                demoted or disabled.
              </Text>
              {mutation.isError && (
                <Text as="p" role="alert">
                  {mutation.error.message}
                </Text>
              )}
              {mutation.isSuccess && (
                <Text as="p" role="status">
                  Account updated.
                </Text>
              )}
              {armed ? (
                <VStack gap={3} role="group" aria-label="Confirm changes">
                  <Text as="p">
                    Apply these changes to <strong>{account.email}</strong>?
                  </Text>
                  <VStack as="ul" gap={3}>
                    {changes.map((change) => (
                      <li key={change}>{change}</li>
                    ))}
                    <li>Sign this account out on all devices</li>
                  </VStack>
                  {(losesAdmin || locksSelfOut) && (
                    <Text as="p">
                      {locksSelfOut
                        ? "This is your own account. You will be signed out and will not be able to sign back in."
                        : "This is your own account. You will lose administrator access, including this page."}
                    </Text>
                  )}
                  <HStack
                    gap={3}
                    wrap="wrap"
                    vAlign="center"
                    hAlign="between"
                  >
                    <Button
                      label={String(
                        mutation.isPending ? "Saving…" : "Save changes",
                      )}
                      variant="primary"
                      type="submit"
                      ref={confirmButton}
                      isDisabled={mutation.isPending}
                      onClick={() => mutation.mutate()}
                    />
                    <Button
                      label={"Keep editing"}
                      variant="secondary"
                      type="button"
                      isDisabled={mutation.isPending}
                      onClick={() => setArmed(false)}
                    />
                  </HStack>
                </VStack>
              ) : (
                <HStack>
                  <Button
                    label={"Review changes"}
                    variant="primary"
                    type="button"
                    isDisabled={mutation.isPending || !changed}
                    onClick={() => setArmed(true)}
                  />
                </HStack>
              )}
            </VStack>
          </fieldset>
        </Form>
      </VStack>
    </Collapsible>
  );
}

export function MyAccount({ current }: { current: Account }) {
  const client = useQueryClient();
  const { search } = useLocation();
  const linkError = new URLSearchParams(search).get("sso_error");
  const identity = useQuery({
    queryKey: ["account-identity", current.id, current.access_revision],
    queryFn: ({ signal }) =>
      read<AccountIdentity>("/api/account/identity", signal),
  });
  const linkIdentity = useMutation({
    mutationFn: () =>
      write<OIDCStart>("/api/account/identity/link", "POST"),
    onSuccess: ({ authorization_url }) =>
      window.location.assign(authorization_url),
  });
  const [oldPassword, setOldPassword] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const mutation = useMutation({
    mutationFn: async () => {
      if (password !== confirm)
        throw new Error("The new passwords do not match.");
      await write("/api/account/password", "POST", {
        current_password: oldPassword,
        password,
      } satisfies PasswordChange);
    },
    onSuccess: async () => {
      setOldPassword("");
      setPassword("");
      setConfirm("");
      await client.resetQueries({ queryKey: ["me"] });
    },
  });
  useEffect(() => {
    document.title = "Review Agent · Your account";
  }, []);
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Your account</Heading>
          <Text as="p">
            {current.email} · {roleLabels[current.role]}
          </Text>
        </VStack>
      </HStack>
      {linkError && (
        <Banner
          status="error"
          title="Organization sign-in was not linked"
          description={
            linkError === "account"
              ? `Use an organization account with the verified email ${current.email}. An existing link cannot be replaced.`
              : "The linking request expired, was cancelled, or could not be verified. Try linking again."
          }
        />
      )}
      {identity.data?.provider_name && (
        <Card padding={6}>
          <VStack gap={3}>
            <Heading level={2}>Organization sign-in</Heading>
            {identity.data.linked ? (
              <Text>
                {identity.data.provider_name} is linked to your account.
              </Text>
            ) : (
              <>
                <Text>
                  Sign in to your organization with the same verified email
                  as this account to link it.
                </Text>
                <Button
                  label={`Link ${identity.data.provider_name}`}
                  variant="secondary"
                  isLoading={linkIdentity.isPending}
                  onClick={() => linkIdentity.mutate()}
                />
              </>
            )}
            {linkIdentity.isError && (
              <Banner
                status="error"
                title="Could not start account linking"
                description={linkIdentity.error.message}
              />
            )}
          </VStack>
        </Card>
      )}
      {identity.isError && (
        <VStack gap={2}>
          <Text role="alert">
            Could not load organization sign-in settings.
          </Text>
          <Button
            label="Retry sign-in settings"
            variant="ghost"
            isLoading={identity.isFetching}
            onClick={() => void identity.refetch()}
          />
        </VStack>
      )}
      <Form
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate();
        }}
      >
        <Heading level={2}>Change password</Heading>
        <Text as="p" color="secondary">
          You will be signed out on all devices after changing it.
        </Text>

        <TextInput
          label={"Current password"}
          type="password"
          isRequired={true}
          value={oldPassword}
          onChange={(value) => setOldPassword(value)}
          {...({
            autoComplete: "current-password",
            required: true,
            maxLength: 128,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />

        <VStack gap={2}>
          <TextInput
            label={"New password"}
            type="password"
            isRequired={true}
            value={password}
            onChange={(value) => setPassword(value)}
            {...({
              autoComplete: "new-password",
              required: true,
              minLength: 15,
              maxLength: 128,
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
          />
          <Text color="secondary">15–128 characters.</Text>
        </VStack>

        <TextInput
          label={"Confirm new password"}
          type="password"
          isRequired={true}
          value={confirm}
          onChange={(value) => setConfirm(value)}
          {...({
            autoComplete: "new-password",
            required: true,
            minLength: 15,
            maxLength: 128,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />

        {mutation.isError && (
          <Text as="p" role="alert">
            {mutation.error.message}
          </Text>
        )}
        <Button
          label={String(
            mutation.isPending ? "Changing password…" : "Change password",
          )}
          variant="primary"
          type="submit"
          isDisabled={mutation.isPending}
        />
      </Form>
    </>
  );
}
