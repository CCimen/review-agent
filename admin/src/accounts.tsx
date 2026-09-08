import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack } from "@astryxdesign/core/Layout";
import { Selector } from "@astryxdesign/core/Selector";
import { Tab, TabList } from "@astryxdesign/core/TabList";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { useLocation } from "react-router-dom";
import { Form } from "./ui";
// Login layout adapted from Astryx's Basic Login template.
// Copyright (c) Meta Platforms, Inc. and affiliates.
import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Center } from "@astryxdesign/core/Center";
import { VStack } from "@astryxdesign/core/Layout";
import { Heading, Text } from "@astryxdesign/core/Text";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitPullRequest } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type {
  Account,
  AccountPage,
  AccountUpdate,
  NewAccount,
  PasswordChange,
} from "./api";
import { login, read, write } from "./api";
import { isAdmin, roleLabels, ScopedAnchor, useScope } from "./scope";
import { Stat } from "./ui";

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
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const mutation = useMutation({
    mutationFn: () => login(email, password),
    onSuccess: async () => {
      setPassword("");
      client.removeQueries({
        predicate: (query) => query.queryKey[0] !== "me",
      });
      await client.resetQueries({ queryKey: ["me"] });
    },
  });
  useEffect(() => {
    document.title = "Review Agent · Sign in";
  }, []);
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
                <Heading level={1}>Sign in</Heading>
                <Text color="secondary">
                  Review activity and administration for your team.
                </Text>
              </VStack>
              <Form
                onSubmit={(event) => {
                  event.preventDefault();
                  mutation.mutate();
                }}
              >
                <VStack gap={4}>
                  <TextInput
                    label={"Email address"}
                    id="login-email"
                    type="email"
                    hasAutoFocus={true}
                    isRequired={true}
                    value={email}
                    onChange={(value) => setEmail(value)}
                    {...({
                      autoComplete: "username",
                      required: true,
                      maxLength: 320,
                    } satisfies InputHTMLAttributes<HTMLInputElement>)}
                  />

                  <TextInput
                    label={"Password"}
                    id="login-password"
                    type="password"
                    isRequired={true}
                    value={password}
                    onChange={(value) => setPassword(value)}
                    {...({
                      autoComplete: "current-password",
                      required: true,
                      maxLength: 128,
                    } satisfies InputHTMLAttributes<HTMLInputElement>)}
                  />

                  {mutation.isError && (
                    <Banner
                      status="error"
                      title="Could not sign in"
                      description={mutation.error.message}
                    />
                  )}
                  <Button
                    label={mutation.isPending ? "Signing in…" : "Sign in"}
                    type="submit"
                    variant="primary"
                    isLoading={mutation.isPending}
                    width="100%"
                  />
                </VStack>
              </Form>
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
  const [reason, setReason] = useState("");
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
        reason,
      } satisfies NewAccount),
    onSuccess: async (account) => {
      setEmail("");
      setReason("");
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
            Platform administration · Manage accounts and platform roles. Team
            roles are assigned in Teams.
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
            review data across the deployment. <strong>Admins</strong> manage
            teams, repositories, and member accounts. <strong>Owners</strong>{" "}
            also manage privileged accounts, provider credentials, and platform
            settings.
          </Text>
        </VStack>
      </Collapsible>
      {createdEmail && (
        <Text as="p" role="status">
          Account created for {createdEmail}.
        </Text>
      )}
      {adding && (
        <Form
          id="add-user"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <Heading level={2}>Add a user</Heading>
          <Grid gap={4} columns={{ minWidth: 240, max: 4, repeat: "fit" }}>
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

          {create.isError && (
            <Text as="p" role="alert">
              {create.error.message}
            </Text>
          )}
          <Button
            label={String(create.isPending ? "Adding user…" : "Add user")}
            variant="primary"
            type="submit"
            isDisabled={create.isPending}
          />
        </Form>
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
                <UserRow key={account.id} account={account} current={current} />
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

function UserRow({ account, current }: { account: Account; current: Account }) {
  const client = useQueryClient();
  const protectedAccount = current.role !== "owner" && isAdmin(account.role);
  const [reason, setReason] = useState("");
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
  }, [role, active, password, reason]);
  useEffect(() => {
    if (armed) confirmButton.current?.focus();
  }, [armed]);
  const mutation = useMutation({
    mutationFn: () =>
      write<Account>(`/api/users/${account.id}`, "PATCH", {
        role,
        active,
        reason,
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
      <VStack gap={4}>
        <Form
          onSubmit={(event) => {
            event.preventDefault();
            if (
              changed &&
              reason.trim() &&
              !protectedAccount &&
              !mutation.isPending
            )
              setArmed(true);
          }}
        >
          {protectedAccount ? (
            <Text as="p">Only a platform owner can change this account.</Text>
          ) : null}
          <fieldset disabled={protectedAccount || mutation.isPending}>
            <VStack gap={4}>
              <Grid gap={4} columns={{ minWidth: 240, max: 4, repeat: "fit" }}>
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
                    mutation.isPending || protectedAccount || mutation.isPending
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
                    mutation.isPending || protectedAccount || mutation.isPending
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
                <VStack
                  gap={3}

                  role="group"
                  aria-label="Confirm changes"
                >
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
                  <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
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
                <Button
                  label={"Review changes"}
                  variant="primary"
                  type="button"
                  isDisabled={mutation.isPending || !changed || !reason.trim()}
                  onClick={() => setArmed(true)}
                />
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
