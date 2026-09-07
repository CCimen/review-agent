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
before(async () => {
  server = await createServer({
    server: { middlewareMode: true },
    appType: "custom",
  });
  ({ ReviewMarkdown } = await server.ssrLoadModule("/src/reviewMarkdown.tsx"));
  ({ ReviewPage } = await server.ssrLoadModule("/src/history.tsx"));
  ({ APIError } = await server.ssrLoadModule("/src/api.ts"));
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
const renderReader = async (client) => {
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
            element: createElement(ReviewPage),
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
