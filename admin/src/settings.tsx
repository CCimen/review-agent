import { DeploymentLink } from "./deployment";
import { useEffect, useState } from "react";
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
    "Maximum active reviews",
    "Maximum queued and running reviews.",
  ],
  [
    "capacity_retry_seconds",
    "Queue recheck delay (seconds)",
    "Delay before checking a full queue again.",
  ],
  [
    "worker_concurrency",
    "Reviews per worker",
    "Simultaneous reviews per worker process.",
  ],
  [
    "job_max_attempts",
    "Maximum review attempts",
    "Attempt budget for newly admitted reviews.",
  ],
  [
    "job_lease_seconds",
    "Worker claim duration (seconds)",
    "Ownership duration renewed by the worker.",
  ],
  [
    "job_heartbeat_seconds",
    "Heartbeat interval (seconds)",
    "Must be less than half the lease duration.",
  ],
  [
    "hermes_timeout_seconds",
    "Review timeout (seconds)",
    "Maximum duration of one Hermes request.",
  ],
  [
    "publish_max_bytes",
    "Publication size limit (bytes)",
    "Between 1,000 and 65,000 bytes.",
  ],
  [
    "publication_max_attempts",
    "Maximum publication attempts",
    "Delivery attempt budget for new publications.",
  ],
] as const;

const advancedGroups = [
  {
    title: "Queue scheduling and recovery",
    fields: [
      [
        "job_priority",
        "Default review priority",
        "Higher priorities run first. Applies to new reviews.",
      ],
      [
        "job_priority_aging_seconds",
        "Priority aging interval (seconds)",
        "Helps older queued reviews progress alongside newer work.",
      ],
      [
        "job_retry_seconds",
        "Review retry delay (seconds)",
        "Wait after a retryable review failure.",
      ],
      [
        "job_poll_seconds",
        "Review queue poll interval (seconds)",
        "How often idle workers look for reviews.",
      ],
      [
        "job_recovery_seconds",
        "Recovery interval (seconds)",
        "How often workers check expired claims.",
      ],
      [
        "job_recovery_batch_size",
        "Recovery batch size",
        "Maximum expired claims handled in one recovery pass.",
      ],
      [
        "admission_max_age_seconds",
        "Maximum admission age (seconds)",
        "How long an incoming review request may wait for admission.",
      ],
    ],
  },
  {
    title: "Publication delivery",
    fields: [
      [
        "publication_lease_seconds",
        "Publication claim duration (seconds)",
        "Ownership duration renewed by the publisher.",
      ],
      [
        "publication_heartbeat_seconds",
        "Publication heartbeat interval (seconds)",
        "Must be less than half the publication claim duration.",
      ],
      [
        "publication_retry_seconds",
        "Publication retry delay (seconds)",
        "Wait before retrying a failed delivery.",
      ],
      [
        "publication_poll_seconds",
        "Publication queue poll interval (seconds)",
        "How often the publisher looks for work.",
      ],
    ],
  },
  {
    title: "Incoming requests and GitHub capacity",
    fields: [
      [
        "github_app_max_body_bytes",
        "Maximum webhook size (bytes)",
        "Reject larger incoming payloads. Maximum 2,097,152 bytes.",
      ],
      [
        "admission_max_concurrent_requests",
        "Concurrent webhook requests",
        "Maximum incoming requests handled at once.",
      ],
      [
        "admission_request_timeout_seconds",
        "Webhook request timeout (seconds)",
        "Maximum time allowed for an incoming request.",
      ],
      [
        "github_gateway_max_concurrent_requests",
        "Concurrent GitHub gateway requests",
        "Maximum requests handled by each gateway process.",
      ],
    ],
  },
] as const;
type NumericField =
  | (typeof numericFields)[number]
  | (typeof advancedGroups)[number]["fields"][number];
type ModelPage = components["schemas"]["ProviderModelPage"];

export function SettingsPage() {
  const query = useQuery({
    queryKey: ["deployment-settings"],
    refetchInterval: false,
    refetchOnWindowFocus: false,
    queryFn: ({ signal }) => read<Page>("/api/settings", signal),
  });
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Settings</h1>
          <p>Manage provider connections, review behavior, and user access.</p>
        </div>
      </div>
      <SettingsTabs />
      <Providers />
      <Freshness query={query} interval={false} />
      {query.data && <SettingsEditor data={query.data} />}
    </>
  );
}

function SettingsEditor({ data }: { data: Page }) {
  const client = useQueryClient();
  const [original, setOriginal] = useState(data);
  const [draft, setDraft] = useState<Settings>(data.settings);
  const [reason, setReason] = useState("");
  const [before, setBefore] = useState<number | null>(null);
  const models = useQuery({
    queryKey: ["provider-models"],
    queryFn: ({ signal }) => read<ModelPage>("/api/providers/models", signal),
    refetchInterval: false,
    staleTime: 300_000,
  });
  const history = useQuery({
    refetchInterval: false,
    refetchOnWindowFocus: false,
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
        expected_revision: original.revision,
        settings: draft,
        reason: reason.trim(),
      }),
    onSuccess: async (revision) => {
      setOriginal({
        ...data,
        settings: revision.settings,
        revision: revision.id,
      });
      setDraft(revision.settings);
      setReason("");
      await client.invalidateQueries({ queryKey: ["deployment-settings"] });
      await client.invalidateQueries({
        queryKey: ["deployment-settings-history"],
      });
    },
  });
  const dirty = JSON.stringify(draft) !== JSON.stringify(original.settings);
  useEffect(() => {
    if (!dirty && data.revision > original.revision) {
      setOriginal(data);
      setDraft(data.settings);
    }
  }, [data, original.revision, dirty]);
  function numericInputs(fields: readonly NumericField[]) {
    return fields.map(([key, label, hint]) => (
      <label className="field" key={key}>
        {label}
        <input
          type="number"
          required
          min={
            key === "publish_max_bytes" ? 1000 : key === "job_priority" ? 0 : 1
          }
          max={
            key === "publish_max_bytes"
              ? 65000
              : key === "github_app_max_body_bytes"
                ? 2097152
                : key === "job_priority"
                  ? 2147483647
                  : undefined
          }
          step={1}
          value={draft[key]}
          onChange={(event) =>
            setDraft({ ...draft, [key]: event.target.valueAsNumber })
          }
        />
        <span className="field-hint">{hint}</span>
      </label>
    ));
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }
  return (
    <>
      <form onSubmit={submit} className="panel settings-panel">
        <div className="panel-heading">
          <h2>Review settings</h2>
          <span className="muted">
            {original.revision
              ? `Revision ${original.revision}`
              : "Environment defaults"}
          </span>
        </div>
        <div className="panel-body">
          <p className="notice">
            Model changes apply to new requests. Queued reviews keep their
            selected model. Other settings take effect after the relevant
            services restart.
          </p>
          <fieldset disabled={save.isPending}>
            <legend>Review model</legend>
            <div className="settings-fields model-fields">
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
                  list="review-model-options"
                  onChange={(e) =>
                    setDraft({ ...draft, model: e.target.value })
                  }
                />
                <span className="field-hint">
                  Suggestions come from Hermes. You can also enter a supported
                  model ID.
                </span>
              </label>
              <datalist id="review-model-options">
                {models.data?.items
                  .filter((item) => item.provider === draft.model_provider)
                  .map((item) => (
                    <option key={item.model} value={item.model} />
                  ))}
              </datalist>
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
                    <option key={value} value={value}>
                      {value === "xhigh"
                        ? "Extra high"
                        : value === "max"
                          ? "Maximum"
                          : value.charAt(0).toUpperCase() + value.slice(1)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </fieldset>
          <fieldset disabled={save.isPending}>
            <legend>Workload and delivery</legend>
            <div className="settings-fields">
              {numericInputs(numericFields)}
            </div>
          </fieldset>
          <fieldset disabled={save.isPending}>
            <legend>Feedback and repository context</legend>
            <div className="settings-fields context-fields">
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
                Include repository code relationships
              </label>
              <label className="field">
                Code graph embeddings
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
          <details className="advanced-settings">
            <summary>Advanced operational settings</summary>
            <p className="field-hint">
              These controls apply after the relevant services restart.
              Environment values provide the initial defaults.
            </p>
            {advancedGroups.map((group) => (
              <fieldset key={group.title} disabled={save.isPending}>
                <legend>{group.title}</legend>
                <div className="settings-fields">
                  {numericInputs(group.fields)}
                </div>
              </fieldset>
            ))}
          </details>
          <label className="field change-reason">
            Reason for change
            <input
              required
              maxLength={500}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <span className="field-hint">
              Required when saving. Describe why this change is needed for the
              history.
            </span>
          </label>
          {save.error && (
            <p className="notice error" role="alert">
              {save.error.message}{" "}
              <button
                type="button"
                className="text-button"
                onClick={() => {
                  setOriginal(data);
                  setDraft(data.settings);
                  setReason("");
                  void client.invalidateQueries({
                    queryKey: ["deployment-settings"],
                  });
                }}
              >
                Reload saved settings and discard edits
              </button>
            </p>
          )}
          <div className="inline-actions">
            <button disabled={!dirty || !reason.trim() || save.isPending}>
              {save.isPending ? "Saving…" : "Save settings"}
            </button>
            <span className="field-hint" role="status">
              {dirty
                ? reason.trim()
                  ? "Changes ready to save"
                  : "Add a reason to save your changes"
                : "No unsaved changes"}
            </span>
            <button
              type="button"
              className="secondary"
              disabled={!dirty || save.isPending}
              onClick={() => {
                setDraft(original.settings);
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
          <h2>Settings loaded by services</h2>
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
                  <th>Policy status</th>
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
                    <td>
                      {(row.revision ?? 0) === data.revision
                        ? "Current at startup"
                        : "Older settings loaded"}
                    </td>
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
          <h2>Settings history</h2>
        </div>
        <div className="panel-body">
          <Freshness query={history} interval={false} />
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
