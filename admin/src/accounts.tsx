import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { login, read, write } from "./api";
import type { Account, AccountUpdate, NewAccount, PasswordChange } from "./api";

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
    <main className="login-page">
      <a className="brand" href="/">
        Review Agent
      </a>
      <h1>Sign in</h1>
      <p className="muted">Review activity and administration for your team.</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate();
        }}
      >
        <label className="field">
          Email address
          <input
            type="email"
            autoComplete="username"
            autoFocus
            required
            maxLength={320}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>
        <label className="field">
          Password
          <input
            type="password"
            autoComplete="current-password"
            required
            maxLength={128}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {mutation.isError && (
          <p className="notice error" role="alert">
            {mutation.error.message}
          </p>
        )}
        <button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="footnote">
        Ask your Review Agent administrator for an account or a password reset.
      </p>
    </main>
  );
}

export function Users({ current }: { current: Account }) {
  const client = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [adding, setAdding] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<NewAccount["role"]>("viewer");
  const query = useQuery({
    queryKey: ["users", offset],
    queryFn: ({ signal }) =>
      read<Account[]>(`/api/users?offset=${offset}`, signal),
  });
  const create = useMutation({
    mutationFn: () =>
      write<Account>("/api/users", "POST", {
        email,
        password,
        role,
      } satisfies NewAccount),
    onSuccess: async () => {
      setEmail("");
      setPassword("");
      setRole("viewer");
      setAdding(false);
      await client.invalidateQueries({ queryKey: ["users"] });
    },
  });
  useEffect(() => {
    document.title = "Review Agent · Users";
  }, []);
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Users</h1>
          <p>Control who can view review activity and manage access.</p>
        </div>
        <button
          onClick={() => {
            setAdding((value) => !value);
            setPassword("");
            create.reset();
          }}
        >
          {adding ? "Cancel adding user" : "Add user"}
        </button>
      </div>
      <p className="notice">
        <strong>Viewer</strong> can read statistics and review history for all
        repositories. <strong>Admin</strong> can also manage accounts. Neither
        role changes reviews or jobs through this panel.
      </p>
      {adding && (
        <form
          className="account-form"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <h2>Add a user</h2>
          <div className="form-fields">
            <label className="field">
              Email address
              <input
                type="email"
                autoComplete="off"
                autoFocus
                required
                maxLength={320}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </label>
            <label className="field">
              Initial password
              <input
                type="password"
                autoComplete="new-password"
                required
                minLength={15}
                maxLength={128}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <span className="field-help">
                15–128 characters. Share it privately with the user.
              </span>
            </label>
            <label className="field">
              Role
              <select
                value={role}
                onChange={(event) =>
                  setRole(event.target.value === "admin" ? "admin" : "viewer")
                }
              >
                <option value="viewer">Viewer</option>
                <option value="admin">Admin</option>
              </select>
            </label>
          </div>
          {create.isError && (
            <p className="notice error" role="alert">
              {create.error.message}
            </p>
          )}
          <button disabled={create.isPending}>
            {create.isPending ? "Adding user…" : "Add user"}
          </button>
        </form>
      )}
      {query.isPending && <p role="status">Loading users…</p>}
      {query.isError && (
        <div className="notice error" role="alert">
          <p>Could not load users.</p>
          <button onClick={() => void query.refetch()}>Retry</button>
        </div>
      )}
      {query.data && (
        <div className="user-list">
          {query.data.slice(0, 50).map((account) => (
            <UserRow key={account.id} account={account} current={current} />
          ))}
        </div>
      )}
      <div className="pagination">
        <button
          className="secondary"
          disabled={offset === 0}
          onClick={() => setOffset((value) => Math.max(0, value - 50))}
        >
          Previous
        </button>
        <span>Page {Math.floor(offset / 50) + 1}</span>
        <button
          className="secondary"
          disabled={!query.data || query.data.length <= 50 || offset >= 10000}
          onClick={() => setOffset((value) => value + 50)}
        >
          Next
        </button>
      </div>
    </>
  );
}

function UserRow({ account, current }: { account: Account; current: Account }) {
  const client = useQueryClient();
  const [role, setRole] = useState(account.role);
  const [active, setActive] = useState(account.active);
  const [password, setPassword] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      write<Account>(`/api/users/${account.id}`, "PATCH", {
        role,
        active,
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
    <details
      className="user-row"
      onToggle={(event) => {
        if (event.currentTarget.open) {
          setRole(account.role);
          setActive(account.active);
          setPassword("");
          mutation.reset();
        }
      }}
    >
      <summary>
        <span>
          <strong>{account.email}</strong>
          {account.id === current.id && (
            <span className="subtext">Your account</span>
          )}
        </span>
        <span>{account.role === "admin" ? "Admin" : "Viewer"}</span>
        <span
          className={`status ${account.active ? "published" : "superseded"}`}
        >
          {account.active ? "Active" : "Disabled"}
        </span>
        <span className="expand-label">Edit</span>
      </summary>
      <form
        className="user-edit"
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate();
        }}
      >
        <div className="form-fields">
          <label className="field">
            Role
            <select
              value={role}
              onChange={(event) =>
                setRole(event.target.value === "admin" ? "admin" : "viewer")
              }
            >
              <option value="viewer">Viewer</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <label className="field">
            Access
            <select
              value={String(active)}
              onChange={(event) => setActive(event.target.value === "true")}
            >
              <option value="true">Active</option>
              <option value="false">Disabled</option>
            </select>
          </label>
          <label className="field">
            Reset password
            <input
              type="password"
              autoComplete="new-password"
              minLength={15}
              maxLength={128}
              placeholder="Leave blank to keep it"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
        </div>
        <p className="field-help">
          Saving changes signs this account out on all devices. At least one
          active administrator must remain.
        </p>
        {mutation.isError && (
          <p className="notice error" role="alert">
            {mutation.error.message}
          </p>
        )}
        {mutation.isSuccess && (
          <p role="status" className="save-result">
            Account updated.
          </p>
        )}
        <button disabled={mutation.isPending}>
          {mutation.isPending ? "Saving…" : "Save changes"}
        </button>
      </form>
    </details>
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
      <div className="page-heading">
        <div>
          <h1>Your account</h1>
          <p>
            {current.email} · {current.role === "admin" ? "Admin" : "Viewer"}
          </p>
        </div>
      </div>
      <form
        className="password-form"
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate();
        }}
      >
        <h2>Change password</h2>
        <p className="muted">
          You will be signed out on all devices after changing it.
        </p>
        <label className="field">
          Current password
          <input
            type="password"
            autoComplete="current-password"
            required
            maxLength={128}
            value={oldPassword}
            onChange={(event) => setOldPassword(event.target.value)}
          />
        </label>
        <label className="field">
          New password
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={15}
            maxLength={128}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <span className="field-help">15–128 characters.</span>
        </label>
        <label className="field">
          Confirm new password
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={15}
            maxLength={128}
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
        </label>
        {mutation.isError && (
          <p className="notice error" role="alert">
            {mutation.error.message}
          </p>
        )}
        <button disabled={mutation.isPending}>
          {mutation.isPending ? "Changing password…" : "Change password"}
        </button>
      </form>
    </>
  );
}
