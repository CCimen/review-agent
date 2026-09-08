import { DeploymentLink } from "./deployment";
import { useState } from "react";
import type { FormEvent } from "react";
import { SettingsTabs } from "./accounts";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { read, write } from "./api";
import type { components } from "./api.generated";
import { Freshness, time } from "./ui";
import { Providers } from "./providers";

type Settings = components["schemas"]["DeploymentSettings"];
type Page = components["schemas"]["DeploymentSettingsPage"];
type Revision = components["schemas"]["SettingsRevision"];
const numericFields = [
  [
    "active_job_limit",
    "Active job limit",
    "Maximum queued and running reviews.",
  ],
  [
    "capacity_retry_seconds",
    "Capacity retry · seconds",
    "Delay before checking a full queue again.",
  ],
  [
    "worker_concurrency",
    "Worker concurrency",
    "Simultaneous reviews per worker process.",
  ],
  [
    "job_max_attempts",
    "Review attempts",
    "Attempt budget for newly admitted reviews.",
  ],
  [
    "job_lease_seconds",
    "Lease · seconds",
    "Ownership duration renewed by the worker.",
  ],
  [
    "job_heartbeat_seconds",
    "Heartbeat · seconds",
    "Must be less than half the lease duration.",
  ],
  [
    "hermes_timeout_seconds",
    "Review timeout · seconds",
    "Maximum duration of one Hermes request.",
  ],
  [
    "publish_max_bytes",
    "Publication limit · bytes",
    "Between 1,000 and 65,000 bytes.",
  ],
  [
    "publication_max_attempts",
    "Publication attempts",
    "Delivery attempt budget for new publications.",
  ],
] as const;

export function SettingsPage() {
  const query = useQuery({
    queryKey: ["deployment-settings"],
    queryFn: ({ signal }) => read<Page>("/api/settings", signal),
  });
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Settings</h1>
          <p>Model connections and deployment policy.</p>
        </div>
      </div>
      <SettingsTabs />
      <Providers />
      <Freshness query={query} />
      {query.data && (
        <SettingsEditor key={query.data.revision} data={query.data} />
      )}
    </>
  );
}

function SettingsEditor({ data }: { data: Page }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Settings>(data.settings);
  const [reason, setReason] = useState("");
  const [before, setBefore] = useState<number | null>(null);
  const history = useQuery({
    queryKey: ["deployment-settings-history", before],
    queryFn: ({ signal }) =>
      read<Page>(
        `/api/settings${before ? `?before_id=${before}` : ""}`,
        signal,
      ),
  });
  const save = useMutation({
    mutationFn: () =>
      write<Revision>("/api/settings", "PUT", {
        expected_revision: data.revision,
        settings: draft,
        reason: reason.trim(),
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["deployment-settings"] });
      await client.invalidateQueries({
        queryKey: ["deployment-settings-history"],
      });
    },
  });
  const dirty = JSON.stringify(draft) !== JSON.stringify(data.settings);
  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }
  return (
    <>
      <form onSubmit={submit} className="panel settings-panel">
        <div className="panel-heading">
          <h2>Deployment policy</h2>
          <span className="muted">
            {data.revision
              ? `Revision ${data.revision}`
              : "Environment defaults"}
          </span>
        </div>
        <div className="panel-body">
          <p className="notice">
            Model changes apply to newly admitted reviews. Queued reviews retain
            their recorded model. Other policy changes load when the relevant
            services restart.
          </p>
          <fieldset disabled={save.isPending}>
            <legend>Review model</legend>
            <div className="settings-fields">
              <label className="field">
                Provider
                <select
                  value={draft.model_provider}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      model_provider: e.target
                        .value as Settings["model_provider"],
                    })
                  }
                >
                  <option value="openai-codex">OpenAI Codex</option>
                  <option value="anthropic">Anthropic</option>
                </select>
              </label>
              <label className="field">
                Model ID
                <input
                  required
                  maxLength={200}
                  value={draft.model}
                  onChange={(e) =>
                    setDraft({ ...draft, model: e.target.value })
                  }
                />
                <span className="field-hint">
                  Use a model supported by the connected provider.
                </span>
              </label>
              <label className="field">
                Reasoning effort
                <select
                  value={draft.reasoning_effort}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      reasoning_effort: e.target
                        .value as Settings["reasoning_effort"],
                    })
                  }
                >
                  {[
                    "none",
                    "minimal",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                  ].map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
            </div>
          </fieldset>
          <fieldset disabled={save.isPending}>
            <legend>Execution and delivery</legend>
            <div className="settings-fields">
              {numericFields.map(([key, label, hint]) => (
                <label className="field" key={key}>
                  {label}
                  <input
                    type="number"
                    required
                    min={key === "publish_max_bytes" ? 1000 : 1}
                    max={key === "publish_max_bytes" ? 65000 : undefined}
                    step={1}
                    value={draft[key]}
                    onChange={(e) =>
                      setDraft({ ...draft, [key]: e.target.valueAsNumber })
                    }
                  />
                  <span className="field-hint">{hint}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset disabled={save.isPending}>
            <legend>Feedback and repository context</legend>
            <div className="settings-fields">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={draft.feedback_enabled}
                  onChange={(e) =>
                    setDraft({ ...draft, feedback_enabled: e.target.checked })
                  }
                />
                Include feedback instructions in reviews
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={draft.code_graph_enabled}
                  onChange={(e) =>
                    setDraft({ ...draft, code_graph_enabled: e.target.checked })
                  }
                />
                Enable code graph context
              </label>
              <label className="field">
                Graph embeddings
                <select
                  value={draft.code_graph_embeddings}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      code_graph_embeddings: e.target
                        .value as Settings["code_graph_embeddings"],
                    })
                  }
                >
                  <option value="none">None</option>
                  <option value="openai">OpenAI</option>
                </select>
                <span className="field-hint">
                  Requires the code graph service; OpenAI embeddings also
                  require its gateway API key.
                </span>
              </label>
            </div>
          </fieldset>
          <label className="field">
            Reason for change
            <input
              required
              maxLength={500}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {save.error && (
            <p className="notice error" role="alert">
              {save.error.message}{" "}
              <button
                type="button"
                className="text-button"
                onClick={() =>
                  client.invalidateQueries({
                    queryKey: ["deployment-settings"],
                  })
                }
              >
                Reload saved settings
              </button>
            </p>
          )}
          <div className="inline-actions">
            <button disabled={!dirty || !reason.trim() || save.isPending}>
              {save.isPending ? "Saving…" : "Save policy"}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={!dirty || save.isPending}
              onClick={() => {
                setDraft(data.settings);
                setReason("");
              }}
            >
              Discard edits
            </button>
          </div>
        </div>
      </form>
      <section className="panel">
        <div className="panel-heading">
          <h2>Service startup observations</h2>
        </div>
        <div className="panel-body">
          <p className="muted">
            Last 50 service starts. These records show which revision was loaded
            at startup; use Health to check current workers. Restart services
            through your deployment platform.
          </p>
          <DeploymentLink />
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Instance</th>
                  <th>Revision</th>
                  <th>Loaded</th>
                </tr>
              </thead>
              <tbody>
                {data.startup_loads.map((row, index) => (
                  <tr key={index}>
                    <td>{row.service}</td>
                    <td>
                      <code>{row.hostname}</code>
                    </td>
                    <td>{row.revision ?? "Environment"}</td>
                    <td>{time(row.loaded_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!data.startup_loads.length && (
            <p className="muted">No startup observations yet.</p>
          )}
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>Policy history</h2>
        </div>
        <div className="panel-body">
          <Freshness query={history} />
          {history.data?.history.map((revision) => (
            <div className="settings-revision" key={revision.id}>
              <div>
                <strong>Revision {revision.id}</strong>
                <p>{revision.reason}</p>
                <span className="muted">
                  {time(revision.created_at)} · {revision.actor}
                </span>
              </div>
              <button
                type="button"
                className="secondary"
                disabled={revision.id === data.revision || save.isPending}
                onClick={() => {
                  setDraft(revision.settings);
                  setReason(`Restore revision ${revision.id}`);
                  window.scrollTo({ top: 0, behavior: "instant" });
                }}
              >
                Restore to editor
              </button>
            </div>
          ))}
          {history.data?.history.length === 0 && (
            <p className="muted">Saving a policy creates its first revision.</p>
          )}
          <div className="pagination">
            <button
              className="secondary"
              disabled={before === null}
              onClick={() => setBefore(null)}
            >
              Newest
            </button>
            <button
              className="secondary"
              disabled={!history.data?.next_before_id}
              onClick={() => setBefore(history.data?.next_before_id ?? null)}
            >
              Older
            </button>
          </div>
        </div>
      </section>
    </>
  );
}
