import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup, renderToReadableStream } from "react-dom/server";
import { createServer } from "vite";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";

let server;
let ReviewMarkdown;
let ReviewPage;
let APIError;
let Users;
before(async () => {
  server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  ({ ReviewMarkdown } = await server.ssrLoadModule("/src/reviewMarkdown.tsx"));
  ({ ReviewPage } = await server.ssrLoadModule("/src/history.tsx"));
  ({ APIError } = await server.ssrLoadModule("/src/api.ts"));
  ({ Users } = await server.ssrLoadModule("/src/accounts.tsx"));
});
after(async () => {
  await server?.close();
});
const render = (markdown) =>
  renderToStaticMarkup(
    createElement(ReviewMarkdown, {
      markdown,
      repository: "example/repository",
      headSha: "a".repeat(40),
    }),
  );

test("published findings retain Markdown, tables, code and native fix-brief disclosure", () => {
  const html = render(
    "## Review\n\n### F1: High · Handle cancellation\n\n[Source](src/job.py)\n\n| Priority | Finding |\n| --- | --- |\n| High | Cancellation |\n\n<details>\n<summary>Fix brief</summary>\n\n```python\nfinally: cleanup()\n```\n\n</details>",
  );
  assert.match(html, /<h3>F1: High · Handle cancellation<\/h3>/);
  assert.match(html, /<table>/);
  assert.match(html, /<details>\s*<summary>Fix brief<\/summary>/);
  assert.match(html, /<code class="language-python">finally: cleanup\(\)/);
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
const renderReader = async (client, role = "viewer") => {
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
          Routes,
          null,
          createElement(Route, {
            path: "/history/:runId",
            element: createElement(ReviewPage, { current: { role } }),
          }),
        ),
      ),
    ),
  );
  await stream.allReady;
  return new Response(stream).text();
};

test("review destination leads with publication and coverage, preserves filters and exposes diagnostics separately", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  client.setQueryData(["review", "24", null], review);
  const html = await renderReader(client);
  assert.match(
    html,
    /href="\/history\?days=7&amp;status=published&amp;before_id=99"/,
  );
  assert.match(html, /#issuecomment-123/);
  assert.match(html, /Limited coverage/);
  assert.match(html, /46 min 24 s/);
  assert.match(html, /aria-current="page"/);
  assert.match(
    html,
    /<details class="execution-details"><summary>Execution details<\/summary>/,
  );
  assert.ok(
    html.indexOf("F1: Handle cancellation") < html.indexOf("Execution details"),
  );
  assert.doesNotMatch(html, /aria-expanded=|class="pr-requests"/);
  client.clear();
});

test("a request removed by retention does not continue displaying its cached publication", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  const queryKey = ["review", "24", null];
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
  client.setQueryData(["review", "24", null], {
    ...review,
    item: queued,
    requests: [queued],
    markdown: null,
  });
  client.setQueryData(["run-controls", 24], {
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
  assert.match(html, /aria-haspopup="dialog"[^>]*>Review actions/);
  assert.match(html, /<summary>Advanced actions<\/summary>/);
  assert.match(html, /Cancel review/);
  assert.doesNotMatch(html, />Retry now<|>Run controls</);
  assert.ok(html.indexOf("Review actions") < html.indexOf("Execution details"));
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
        createElement(Users, { current: { role: "admin" } }),
      ),
    ),
  );
  assert.match(html, /aria-label="Settings views"/);
  assert.match(html, /href="\/settings"/);
  assert.match(
    html,
    /aria-current="page"[^>]*href="\/users"[^>]*>Users &amp; roles/,
  );
  assert.match(html, /Add user/);
  assert.match(html, /Accounts/);
  assert.match(html, />12</);
  assert.doesNotMatch(html, /Neither role changes reviews or jobs/);
  client.clear();
});
