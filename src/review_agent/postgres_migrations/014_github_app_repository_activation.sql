ALTER TABLE review_agent.github_app_installations
    ADD COLUMN repository_activation_policy TEXT NOT NULL DEFAULT 'explicit',
    ADD COLUMN activation_policy_actor TEXT,
    ADD COLUMN activation_policy_reason TEXT,
    ADD COLUMN activation_policy_changed_at TIMESTAMPTZ,
    ADD CONSTRAINT github_app_installations_activation_policy_ck CHECK (
        repository_activation_policy IN ('explicit', 'automatic')
    ),
    ADD CONSTRAINT github_app_installations_activation_audit_ck CHECK (
        (
            activation_policy_actor IS NULL
            AND activation_policy_reason IS NULL
            AND activation_policy_changed_at IS NULL
        )
        OR (
            activation_policy_actor IS NOT NULL
            AND activation_policy_reason IS NOT NULL
            AND activation_policy_changed_at IS NOT NULL
            AND btrim(activation_policy_actor) <> ''
            AND char_length(activation_policy_actor) <= 120
            AND btrim(activation_policy_reason) <> ''
            AND char_length(activation_policy_reason) <= 500
        )
    ),
    ADD CONSTRAINT github_app_installations_automatic_approval_ck CHECK (
        repository_activation_policy <> 'automatic'
        OR activation_policy_changed_at IS NOT NULL
    );

ALTER TABLE review_agent.github_app_repository_access
    ADD COLUMN automatic_activation_blocked BOOLEAN NOT NULL DEFAULT false;

WITH latest_operator_transition AS (
    SELECT DISTINCT ON (repository_id)
           repository_id, event_kind
    FROM review_agent.github_app_repository_access_events
    WHERE event_kind IN ('enabled', 'disabled')
    ORDER BY repository_id, recorded_at DESC, id DESC
)
UPDATE review_agent.github_app_repository_access AS access
SET automatic_activation_blocked = true,
    trigger_mode = 'manual'
FROM latest_operator_transition AS transition
WHERE transition.repository_id = access.repository_id
  AND transition.event_kind = 'disabled';

ALTER TABLE review_agent.github_app_repository_access
    ADD CONSTRAINT github_app_repository_access_activation_block_ck CHECK (
        NOT automatic_activation_blocked OR NOT enabled
    );
