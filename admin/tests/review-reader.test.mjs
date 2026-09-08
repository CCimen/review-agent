import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup, renderToReadableStream } from "react-dom/server";
import { createServer } from "vite";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { readFile } from "node:fs/promises";

let server;
let ReviewMarkdown;
let ReviewPage;
let APIError;
let Users;
let Access;
let SettingsPage;
let QualityPage;
let HermesHealth;
let settingsDefaults;
let ScopeProvider;
let contextualTo;
let AuditLog;
let AuditJSON;
let ConnectionQuota;
let IntegrationsPage;
const account = (role = "owner", access_revision = 0) => ({
  id: "test-account",
  email: "owner@example.test",
  active: true,
  role,
  access_revision,
});
const scopedKey = (key, role = "viewer", revision = 0) => [
  ...key,
  "scoped",
  `test-account:${role}:${revision}:all:`,
];
before(async () => {
  server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  ({ ReviewMarkdown } = await server.ssrLoadModule("/src/reviewMarkdown.tsx"));
  ({ ScopeProvider, contextualTo } =
    await server.ssrLoadModule("/src/scope.tsx"));
  ({ ReviewPage } = await server.ssrLoadModule("/src/history.tsx"));
  ({ APIError } = await server.ssrLoadModule("/src/api.ts"));
  ({ Users } = await server.ssrLoadModule("/src/accounts.tsx"));
  ({ Access } = await server.ssrLoadModule("/src/access.tsx"));
  ({ SettingsPage } = await server.ssrLoadModule("/src/settings.tsx"));
  ({ QualityPage } = await server.ssrLoadModule("/src/quality.tsx"));
  ({ HermesHealth } = await server.ssrLoadModule("/src/deployment.tsx"));
  ({ AuditLog, AuditJSON } = await server.ssrLoadModule("/src/audit.tsx"));
  ({ ConnectionQuota } = await server.ssrLoadModule("/src/modelQuota.tsx"));
  ({ IntegrationsPage } = await server.ssrLoadModule("/src/integrations.tsx"));
  const contract = JSON.parse(
    await readFile(new URL("../openapi.json", import.meta.url), "utf8"),
  );
  settingsDefaults = Object.fromEntries(
    Object.entries(
      contract.components.schemas.DeploymentSettings.properties,
    ).map(([key, value]) => [key, value.default]),
  );
});
after(async () => {
  await server?.close();
});
const elements = (html, tag) =>
  html.match(new RegExp(`<${tag}\\b[^>]*>[\\s\\S]*?</${tag}>`, "g")) ?? [];
const textContent = (html) => html.replace(/<[^>]+>/g, "");
const button = (html, label) =>
  elements(html, "button").find((item) => textContent(item).includes(label)) ??
  "";

const render = (markdown) =>
  renderToStaticMarkup(
    createElement(ReviewMarkdown, {
      markdown,
      repository: "example/repository",
      headSha: "a".repeat(40),
    }),
  );

const renderConsolePage = (component, cache) => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  for (const [key, value] of cache)
    client.setQueryData(
      key[0] === "quality" ? scopedKey(key, "owner") : key,
      value,
    );
  const html = renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client },
      createElement(
        MemoryRouter,
        null,
        createElement(ScopeProvider, { current: account() }, component),
      ),
    ),
  );
  client.clear();
  return html;
};

test("integration management distinguishes report grants, content grants and revoked credentials", () => {
  const html = renderConsolePage(createElement(IntegrationsPage), [
    [
      scopedKey(["integrations", 0], "owner"),
      {
        items: [
          {
            id: 1,
            name: "Metrics <script>consumer</script>",
            deployment_wide: false,
            read_review_content: false,
            created_at: "2026-09-08T12:00:00Z",
            expires_at: "2026-10-08T12:00:00Z",
            revoked_at: null,
            teams: [{ id: 1, name: "Payments" }],
            state: "active",
          },
          {
            id: 2,
            name: "Retired consumer",
            deployment_wide: true,
            read_review_content: true,
            created_at: "2026-09-08T12:00:00Z",
            expires_at: "2026-10-08T12:00:00Z",
            revoked_at: "2026-09-08T13:00:00Z",
            teams: [],
            state: "revoked",
          },
        ],
        next_after_id: null,
      },
    ],
  ]);
  assert.match(html, /Users &amp; roles/);
  assert.match(html, /Metrics &lt;script&gt;consumer&lt;\/script&gt;/);
  assert.match(html, /Payments/);
  assert.match(html, /Reports only/);
  assert.match(html, /Reports and published content/);
  assert.match(html, /Entire deployment/);
  assert.match(html, /Revoked/);
  assert.equal(
    elements(html, "button").filter((item) => textContent(item) === "Revoke")
      .length,
    1,
  );
  assert.match(html, /Permissions are fixed/);
  assert.match(html, /Open the API reference/);
});

test("account quota preserves actual windows, small percentages, and shared scope", () => {
  const connection = {
    id: 1,
    revision: 2,
    team_id: null,
    state: "enabled",
    accounts: [{ provider: "openai-codex", revision: 1, verified: true }],
  };
  const html = renderConsolePage(
    createElement(ConnectionQuota, { connection }),
    [
      [
        scopedKey(["model-quota", 1, 2, 1], "owner"),
        {
          provider: "openai-codex",
          refreshing: false,
          stale: true,
          next_refresh_at: null,
          unavailable_reason: "provider_unavailable",
          snapshot: {
            fetched_at: 1788897600,
            plan: "Pro",
            reset_credits_available: 0,
            limit_reached_type: null,
            spend_control_reached: null,
            buckets: [
              {
                id: "codex",
                name: null,
                normal_model_slug: null,
                allowed: true,
                limit_reached: false,
                windows: [
                  {
                    kind: "primary",
                    used_percent: 0.5,
                    duration_seconds: 7200,
                    resets_at: 1900000000,
                  },
                  {
                    kind: "secondary",
                    used_percent: null,
                    duration_seconds: 259200,
                    resets_at: null,
                  },
                ],
              },
              {
                id: "other",
                name: "Other <script>bucket</script>",
                normal_model_slug: "other-model",
                allowed: null,
                limit_reached: null,
                windows: [],
              },
            ],
          },
        },
      ],
    ],
  );
  assert.match(html, /includes usage by other teams/);
  assert.match(html, /99.5%/);
  assert.match(html, /0.5% used/);
  assert.match(html, /2 hours/);
  assert.match(html, /3 days/);
  assert.match(html, /Available usage resets: 0/);
  assert.match(html, /Stale observation/);
  assert.match(html, /Unknown/);
  assert.match(html, /Other &lt;script&gt;bucket&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>|Weekly|Session quota/);
});

test("quota without provider data remains unknown", () => {
  const connection = {
    id: 2,
    revision: 1,
    team_id: 7,
    state: "enabled",
    accounts: [{ provider: "openai-codex", revision: 1, verified: true }],
  };
  const html = renderConsolePage(
    createElement(ConnectionQuota, { connection }),
    [
      [
        scopedKey(["model-quota", 2, 1, 1], "owner"),
        {
          provider: "openai-codex",
          refreshing: false,
          stale: true,
          next_refresh_at: null,
          unavailable_reason: "provider_unavailable",
          snapshot: null,
        },
      ],
    ],
  );
  assert.match(html, /Quota is currently unknown/);
  assert.doesNotMatch(html, /0%|Available usage resets: 0/);
});

test("audit requires a purpose and justification before rendering records", () => {
  const html = renderConsolePage(createElement(AuditLog), []);
  assert.match(html, /Explain why you need access/);
  assert.match(html, /Purpose/);
  assert.match(html, /role="combobox"/);
  assert.match(html, /Select a purpose/);
  assert.match(button(html, "Access audit log"), /disabled=""/);
  assert.match(html, /minLength="10"/);
  assert.match(html, /0\/500/);
  assert.doesNotMatch(html, /Export matching events|Audit events<\/table>/);
});

test("audit event JSON preserves nested values and escapes untrusted content", () => {
  const event = {
    id: 42,
    action: "audit_viewed",
    reason: "Investigate <script>unsafe</script>",
    details: { purpose: "access_review", returned_count: 5 },
  };
  const html = renderToStaticMarkup(createElement(AuditJSON, { event }));
  assert.match(html, /View JSON · #42/);
  assert.match(html, /Copy JSON/);
  assert.match(textContent(html), /&quot;returned_count&quot;: 5/);
  assert.match(
    textContent(html),
    /&quot;purpose&quot;: &quot;access_review&quot;/,
  );
  assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("access distinguishes GitHub scope from review activation and preserves disabled repositories", () => {
  const capability = {
    configured: true,
    default_profile: "default-standard",
    detail: "Configured",
  };
  const html = renderConsolePage(createElement(Access), [
    [
      ["access", "installations", 0],
      {
        capability,
        next_after_id: null,
        items: [
          {
            installation_id: 12,
            account: "example",
            account_type: "organization",
            repository_selection: "all",
            repository_activation: "explicit",
            status: "active",
            contents_permission: "read",
            issues_permission: "write",
            pull_requests_permission: "write",
            updated_at: "2026-09-08T08:00:00Z",
            activation_policy_changed_at: null,
          },
        ],
      },
    ],
    [
      ["access", "repositories", 0],
      {
        capability,
        next_after_id: null,
        items: [
          {
            repository_id: 5,
            repository: "example/repository",
            access: "available",
            enabled: false,
            automatic_activation_blocked: true,
            profile: null,
          },
        ],
      },
    ],
  ]);
  assert.match(html, /GitHub grants access to/);
  assert.match(html, /All repositories/);
  assert.match(html, /Only explicitly enabled repositories/);
  assert.match(html, /Automatic activation blocked/);
  assert.match(html, /Add repository/);
  assert.match(html, /value="default-standard"/);
  assert.match(html, /Check live GitHub status/);
});

test("settings expose saved operational defaults and distinguish older startup observations", () => {
  const data = {
    settings: { ...settingsDefaults, job_retry_seconds: 47 },
    revision: 2,
    history: [],
    next_before_id: null,
    startup_loads: [
      {
        service: "publisher",
        hostname: "publisher-1",
        revision: 1,
        loaded_at: "2026-09-08T08:00:00Z",
      },
    ],
  };
  const html = renderConsolePage(createElement(SettingsPage), [
    [["deployment-settings"], data],
    [["deployment-settings-history", null], data],
    [
      ["provider-models"],
      {
        capability: { configured: true },
        items: [{ provider: "openai-codex", model: "gpt-demo" }],
      },
    ],
  ]);
  assert.match(
    button(html, "Advanced operational settings"),
    /aria-expanded="false"/,
  );
  assert.match(html, /value="47"/);
  assert.match(html, /<datalist[^>]*><option value="gpt-demo"/);
  assert.match(html, /Older settings loaded/);
  assert.match(html, /No unsaved changes/);
});

test("quality shows feedback denominators and an empty report state without implying accuracy", () => {
  const html = renderConsolePage(
    createElement(QualityPage, { current: { role: "viewer" } }),
    [
      [
        ["quality", "report", "days=30"],
        {
          published_findings: 12,
          false_positive_signals: { count: 0, denominator: 12 },
          scope_confusion_signals: { count: 0, denominator: 3 },
          missed_issue_signals: { count: 0, denominator: 3 },
          triage_backlog: 0,
          oldest_triage_backlog_seconds: null,
          cohorts: [],
        },
      ],
      [
        ["quality", "feedback", "limit=50&offset=0"],
        { items: [], total: 0, pending: 0, next_offset: null, offset: 0 },
      ],
    ],
  );
  assert.match(html, /12 published findings in this period/);
  assert.match(html, /3 completed reviews in this period/);
  assert.match(html, /No missed issues reported/);
  assert.match(html, /without feedback have not been assessed/);
  assert.match(html, /href="\/history\?days=30&amp;status=published"/);
});

test("engine readiness remains distinct from a successful provider request", () => {
  const html = renderConsolePage(createElement(HermesHealth), [
    [
      ["hermes-runtime"],
      {
        capability: { configured: true },
        runtime: {
          status: "degraded",
          version: "0.21.0",
          model: "gpt-test",
          active_agents: 2,
          busy: true,
          drainable: true,
          chat_available: true,
          checks: [{ name: "disk", status: "degraded" }],
        },
      },
    ],
  ]);
  assert.match(html, /Needs attention/);
  assert.match(html, /Disk space/);
  assert.match(html, /0\.21\.0/);
  assert.match(html, /They do not send a model request/);
});

test("published findings retain Markdown, tables, code and native fix-brief disclosure", () => {
  const html = render(
    "## Review\n\n### F1: High · Handle cancellation\n\n[Source](src/job.py)\n\n| Priority | Finding |\n| --- | --- |\n| High | Cancellation |\n\n<details>\n<summary>Fix brief</summary>\n\n```python\nfinally: cleanup()\n```\n\n</details>",
  );
  assert.match(html, /<h3[^>]*>F1: High · Handle cancellation<\/h3>/);
  assert.match(html, /<table[^>]*aria-label="Review table"/);
  assert.match(button(html, "Fix brief"), /aria-expanded="false"/);
  assert.match(textContent(html), /finally: cleanup\(\)/);
  assert.match(
    html,
    /href="https:\/\/github.com\/example\/repository\/blob\/a{40}\/src\/job.py"/,
  );
  assert.match(html, /opens in a new tab/);
});

test("untrusted published content cannot execute HTML, submit forms or load remote images", () => {
  const html = render(
    '## Finding\n\n<script>alert(1)</script>\n\n<img src="https://tracker.test/pixel" onerror="alert(1)">\n\n[unsafe](javascript:alert%281%29)\n\n<a href="data:text/html,attack" onclick="alert(1)">link</a>\n\n<iframe src="https://tracker.test/frame"></iframe>\n\n<form action="/api/users"><input name="role" value="admin"><button>Submit</button></form>\n\n![tracking](https://tracker.test/markdown-pixel)\n\n<p style="position:fixed" id="main">Visible evidence</p>',
  );
  assert.match(html, /Visible evidence/);
  assert.doesNotMatch(html, /<(script|img|iframe|form|input|button)\b/);
  assert.doesNotMatch(
    html,
    /\b(onerror|onclick|style)=|href="(?:javascript|data):|id="main"/,
  );
  assert.doesNotMatch(html, /tracker\.test/);
});

const request = {
  id: 24,
  repository: "example/repository",
  pr_number: 721,
  state: "published",
  phase: "posted",
  findings_count: 3,
  started_at: "2026-09-07T12:00:00Z",
  posted_at: "2026-09-07T12:46:24Z",
  completed_at: "2026-09-07T12:46:24Z",
  last_heartbeat_at: "2026-09-07T12:46:24Z",
  head_sha: "a".repeat(40),
  base_sha: "b".repeat(40),
  previous_head_sha: "c".repeat(40),
  failure_code: null,
  job_failure_code: null,
  recovered: false,
  is_latest: true,
  publication_superseded: false,
  attempt_count: 1,
  max_attempts: 3,
  coverage: {
    state: "partial",
    registration_complete: true,
    changed_files_reported: 237,
    changed_files_registered: 237,
    changed_paths_with_complete_diff: 213,
  },
};
const review = {
  item: request,
  markdown: "### F1: Handle cancellation",
  content_truncated: false,
  publication_links: [
    {
      label: "Open published review on GitHub",
      url: "https://github.com/example/repository/pull/721#issuecomment-123",
    },
  ],
  links_truncated: false,
  requests: [request],
  next_cursor: null,
};
const renderReader = async (client, role = "viewer", revision = 0) => {
  const stream = await renderToReadableStream(
    createElement(
      QueryClientProvider,
      { client },
      createElement(
        MemoryRouter,
        {
          initialEntries: ["/history/24?days=7&status=published&before_id=99"],
        },
        createElement(
          ScopeProvider,
          { current: account(role, revision) },
          createElement(
            Routes,
            null,
            createElement(Route, {
              path: "/history/:runId",
              element: createElement(ReviewPage),
            }),
          ),
        ),
      ),
    ),
  );
  await stream.allReady;
  return new Response(stream).text();
};

test("queued review explains account quota waiting and the next check", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const queued = {
    ...request,
    state: "queued",
    posted_at: null,
    completed_at: null,
    quota_wait_until: "2026-09-08T15:40:00Z",
    attempt_count: 0,
  };
  client.setQueryData(scopedKey(["review", "24", null]), {
    ...review,
    item: queued,
    markdown: null,
    requests: [queued],
  });
  const html = await renderReader(client);
  assert.match(html, /Waiting for account quota/);
  assert.match(html, /next quota check is due/);
  assert.match(html, /provider confirms quota is available/);
  client.clear();
});

test("review destination leads with publication and coverage, preserves filters and exposes diagnostics separately", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  client.setQueryData(scopedKey(["review", "24", null]), review);
  const html = await renderReader(client);
  assert.match(
    html,
    /href="\/history\?days=7&amp;status=published&amp;before_id=99"/,
  );
  assert.match(html, /#issuecomment-123/);
  assert.match(html, /Limited coverage/);
  assert.match(html, /46 min 24 s/);
  assert.match(html, /aria-current="page"/);
  assert.match(button(html, "Execution details"), /aria-expanded="false"/);
  assert.ok(
    html.indexOf("F1: Handle cancellation") < html.indexOf("Execution details"),
  );
  assert.doesNotMatch(html, /class="pr-requests"/);
  client.clear();
});

test("a request removed by retention does not continue displaying its cached publication", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const queryKey = scopedKey(["review", "24", null]);
  client.setQueryData(queryKey, review);
  await assert.rejects(
    client.fetchQuery({
      queryKey,
      queryFn: async () => {
        throw new APIError(404);
      },
    }),
  );
  const html = await renderReader(client);
  assert.match(html, /Review request not found/);
  assert.doesNotMatch(html, /F1: Handle cancellation|Recorded publication/);
  client.clear();
});

test("queued reviews expose contextual admin actions without premature coverage or duplicate waiting content", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const queued = {
    ...request,
    state: "queued",
    phase: "accepted",
    posted_at: null,
    completed_at: null,
    coverage: {
      ...request.coverage,
      registration_complete: true,
      changed_paths_with_complete_diff: 0,
    },
  };
  client.setQueryData(scopedKey(["review", "24", null], "admin"), {
    can_maintain: true,
    ...review,
    item: queued,
    requests: [queued],
    markdown: null,
  });
  client.setQueryData(scopedKey(["run-controls", 24], "admin"), {
    run: {
      id: 24,
      status: "running",
      phase: "accepted",
      last_heartbeat_at: request.last_heartbeat_at,
    },
    job: {
      id: 1,
      status: "queued",
      lease_generation: 0,
      available_at: request.started_at,
    },
    actions: {
      release_retry: {
        available: false,
        reason: "Only delayed retries can be released.",
      },
      cancel: { available: true, reason: "Cancel this review." },
      mark_stalled: {
        available: true,
        reason: "Mark failed if the worker heartbeat is stale.",
      },
    },
    audit: [],
  });
  const html = await renderReader(client, "admin");
  assert.match(html, /Waiting for a worker/);
  assert.doesNotMatch(
    html,
    /Complete diffs were available|may leave changes unreviewed|Review not published yet/,
  );
  assert.match(html, /aria-haspopup="dialog"[^>]*>[\s\S]*?Review actions/);
  assert.match(button(html, "Advanced actions"), /aria-expanded="false"/);
  assert.match(html, /Cancel review/);
  assert.doesNotMatch(html, />Retry now<|>Run controls</);
  assert.ok(html.indexOf("Review actions") < html.indexOf("Execution details"));
  client.setQueryData(scopedKey(["review", "24", null]), {
    ...review,
    item: queued,
    requests: [queued],
    can_maintain: false,
    markdown: null,
  });
  const viewer = await renderReader(client);
  assert.doesNotMatch(viewer, /Review actions|Cancel review/);
  client.clear();
});

test("user management has Settings navigation and retains account totals and creation", () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  client.setQueryData(["users", 0], {
    items: [],
    total: 12,
    admin_count: 3,
    disabled_count: 2,
    has_more: false,
  });
  const html = renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client },
      createElement(
        MemoryRouter,
        { initialEntries: ["/users"] },
        createElement(
          ScopeProvider,
          { current: account() },
          createElement(Users, { current: account() }),
        ),
      ),
    ),
  );
  assert.match(html, /aria-label="Settings views"/);
  assert.match(html, /href="\/settings"/);
  assert.match(
    html,
    /<a(?=[^>]*aria-current="(?:page|true)")(?=[^>]*href="\/users")[^>]*>[\s\S]*?Users &amp; roles[\s\S]*?<\/a>/,
  );
  assert.match(html, /Add user/);
  assert.match(html, /Accounts/);
  assert.match(html, />12</);
  assert.doesNotMatch(html, /Neither role changes reviews or jobs/);
  client.clear();
});

test("navigation keeps team and reporting period while leaving page filters local", () => {
  assert.deepEqual(
    contextualTo(
      "/quality?repository=example%2Frepository",
      "?team_id=12&days=7&status=failed&before_id=99",
    ),
    {
      pathname: "/quality",
      search: "?repository=example%2Frepository&team_id=12&days=7",
    },
  );
  assert.equal(
    contextualTo("https://github.com/example/repository", "?team_id=12"),
    "https://github.com/example/repository",
  );
});

test("a membership revision cannot reuse the previous scope's cached review", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  client.setQueryData(scopedKey(["review", "24", null]), review);
  const before = await renderReader(client);
  assert.match(before, /F1: Handle cancellation/);
  const after = await renderReader(client, "viewer", 1);
  assert.doesNotMatch(after, /F1: Handle cancellation|Recorded publication/);
  client.clear();
});

test("members with one team need no selector; multiple teams and platform admins can switch", async () => {
  const { ScopeSelector } = await server.ssrLoadModule("/src/scope.tsx");
  const renderSelector = (role, teams) => {
    const current = account(role);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    client.setQueryData(["teams", "selector", `test-account:${role}:0`], {
      total: teams.length,
      items: teams,
      next_after_id: null,
    });
    const html = renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client },
        createElement(
          MemoryRouter,
          { initialEntries: ["/teams"] },
          createElement(
            ScopeProvider,
            { current },
            createElement(ScopeSelector),
          ),
        ),
      ),
    );
    client.clear();
    return html;
  };
  const teams = [
    { id: 10, name: "Payments", repository_count: 3, role: "viewer" },
    { id: 20, name: "Service desk", repository_count: 1, role: "maintainer" },
  ];
  const single = renderSelector("member", teams.slice(0, 1));
  assert.match(single, /Payments/);
  assert.doesNotMatch(
    single,
    /Switch team|popoverTarget|popovertarget|All teams/,
  );
  const multiple = renderSelector("member", teams);
  assert.match(multiple, /Switch team/);
  assert.match(multiple, /All my teams/);
  for (const role of ["admin", "owner"]) {
    const platform = renderSelector(role, teams.slice(0, 1));
    assert.match(platform, /Switch team/);
    assert.match(platform, /All teams/);
    assert.match(platform, /aria-haspopup="dialog"/);
  }
});

test("activity keeps request metadata and does not report unpublished findings", async () => {
  const { ActivityPage } = await server.ssrLoadModule("/src/activity.tsx");
  const html = renderConsolePage(createElement(ActivityPage), [
    [
      scopedKey(["activity", "days=30&status=all&limit=50"], "owner"),
      {
        items: [
          { ...request, findings_count: 7 },
          {
            ...request,
            id: 25,
            state: "failed",
            phase: "failed",
            posted_at: null,
            findings_count: 99,
            max_attempts: null,
            failure_code: "provider_error",
            recovered: true,
          },
        ],
        total: 2,
        next_cursor: 24,
      },
    ],
  ]);
  assert.match(html, /aria-label="Review requests"/);
  for (const label of [
    "Pull request",
    "State",
    "Progress",
    "Commit",
    "Findings",
    "Attempts used",
    "Last activity",
  ])
    assert.match(html, new RegExp(`>${label}<`));
  assert.match(html, /href="\/history\/24"/);
  assert.match(html, />Published<\/span>/);
  assert.match(html, />Failed<\/span>/);
  assert.match(html, />7<\/td>/);
  assert.doesNotMatch(html, />99<\/td>/);
  assert.match(html, /The model provider returned an error/);
  assert.match(html, /Recovered: a later review published/);
  assert.match(html, /Older requests/);
});

test("login retains native validation and password manager attributes", async () => {
  const { Login } = await server.ssrLoadModule("/src/accounts.tsx");
  const html = renderConsolePage(createElement(Login), []);
  for (const [type, autocomplete, maxLength] of [
    ["email", "username", 320],
    ["password", "current-password", 128],
  ]) {
    const input = (html.match(/<input\b[^>]*>/g) ?? []).find((tag) =>
      tag.includes(`type="${type}"`),
    );
    assert.ok(input);
    assert.match(input, new RegExp(`autoComplete="${autocomplete}"`));
    assert.match(input, /required=""/);
    assert.match(input, new RegExp(`maxLength="${maxLength}"`));
  }
  assert.match(html, /<button[^>]*type="submit"/);
});

test("console shell preserves role-based navigation without browser globals", async () => {
  const { ConsoleLayout } = await server.ssrLoadModule("/src/console.tsx");
  for (const role of ["owner", "admin", "viewer"]) {
    const html = renderConsolePage(
      createElement(
        ConsoleLayout,
        {
          current: account(role),
          theme: "light",
          toggleTheme() {},
          logout() {},
          signingOut: false,
        },
        createElement("h1", null, "Page content"),
      ),
      [],
    );
    assert.match(html, /Page content/);
    assert.match(html, /href="\/repositories"/);
    assert.equal(html.includes('href="/settings"'), role === "owner");
    assert.equal(html.includes('href="/audit"'), role !== "viewer");
    assert.equal(html.includes('href="/users"'), role !== "viewer");
  }
});

test("forms block invalid numeric drafts and submit valid values", async () => {
  const { Form } = await server.ssrLoadModule("/src/ui.tsx");
  let submitted = 0;
  let prevented = 0;
  let focused = 0;
  const form = Form({ onSubmit: () => submitted++ });
  const event = (invalid) => ({
    preventDefault: () => prevented++,
    currentTarget: {
      querySelector: (selector) => {
        assert.equal(selector, '[aria-invalid="true"]');
        return invalid ? { focus: () => focused++ } : null;
      },
    },
  });
  form.props.onSubmit(event(true));
  assert.equal(submitted, 0);
  assert.equal(prevented, 1);
  assert.equal(focused, 1);
  form.props.onSubmit(event(false));
  assert.equal(submitted, 1);
});
