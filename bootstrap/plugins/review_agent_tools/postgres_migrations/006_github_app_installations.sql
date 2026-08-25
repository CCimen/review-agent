CREATE TABLE review_agent.github_app_installations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider_installation_id BIGINT NOT NULL,
    account_id BIGINT NOT NULL,
    account_login TEXT NOT NULL,
    account_type TEXT NOT NULL,
    repository_selection TEXT NOT NULL,
    status TEXT NOT NULL,
    contents_permission TEXT NOT NULL,
    issues_permission TEXT NOT NULL,
    pull_requests_permission TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    suspended_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    CONSTRAINT github_app_installations_provider_id_uk
        UNIQUE (provider_installation_id),
    CONSTRAINT github_app_installations_identity_ck CHECK (
        provider_installation_id > 0
        AND account_id > 0
        AND btrim(account_login) <> ''
        AND char_length(account_login) <= 100
    ),
    CONSTRAINT github_app_installations_account_type_ck
        CHECK (account_type IN ('user', 'organization')),
    CONSTRAINT github_app_installations_selection_ck
        CHECK (repository_selection IN ('selected', 'all')),
    CONSTRAINT github_app_installations_permissions_ck CHECK (
        contents_permission IN ('none', 'read', 'write')
        AND issues_permission IN ('none', 'read', 'write')
        AND pull_requests_permission IN ('none', 'read', 'write')
    ),
    CONSTRAINT github_app_installations_lifecycle_ck CHECK (
        (
            status = 'active'
            AND suspended_at IS NULL
            AND deleted_at IS NULL
        )
        OR (
            status = 'suspended'
            AND suspended_at IS NOT NULL
            AND deleted_at IS NULL
        )
        OR (
            status = 'deleted'
            AND deleted_at IS NOT NULL
        )
    ),
    CONSTRAINT github_app_installations_timestamps_ck CHECK (
        updated_at >= created_at
        AND (suspended_at IS NULL OR suspended_at >= created_at)
        AND (deleted_at IS NULL OR deleted_at >= created_at)
    )
);

CREATE TABLE review_agent.github_app_repository_access (
    repository_id BIGINT PRIMARY KEY,
    installation_id BIGINT NOT NULL,
    access_state TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT false,
    trigger_mode TEXT NOT NULL DEFAULT 'manual',
    profile_key TEXT,
    enabled_at TIMESTAMPTZ,
    disabled_at TIMESTAMPTZ,
    updated_by TEXT NOT NULL,
    update_reason TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT github_app_repository_access_repository_fk
        FOREIGN KEY (repository_id) REFERENCES review_agent.repositories(id),
    CONSTRAINT github_app_repository_access_installation_fk
        FOREIGN KEY (installation_id)
        REFERENCES review_agent.github_app_installations(id),
    CONSTRAINT github_app_repository_access_state_ck CHECK (
        access_state IN (
            'available', 'removed', 'installation_suspended',
            'installation_deleted'
        )
    ),
    CONSTRAINT github_app_repository_access_trigger_ck
        CHECK (trigger_mode IN ('manual', 'automatic')),
    CONSTRAINT github_app_repository_access_profile_ck CHECK (
        profile_key IS NULL
        OR profile_key ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'
    ),
    CONSTRAINT github_app_repository_access_actor_ck CHECK (
        btrim(updated_by) <> '' AND char_length(updated_by) <= 120
    ),
    CONSTRAINT github_app_repository_access_reason_ck CHECK (
        btrim(update_reason) <> '' AND char_length(update_reason) <= 500
    ),
    CONSTRAINT github_app_repository_access_enabled_ck CHECK (
        (
            enabled
            AND access_state = 'available'
            AND profile_key IS NOT NULL
            AND enabled_at IS NOT NULL
            AND disabled_at IS NULL
        )
        OR NOT enabled
    ),
    CONSTRAINT github_app_repository_access_timestamps_ck CHECK (
        (disabled_at IS NULL OR enabled_at IS NULL OR disabled_at >= enabled_at)
    )
);

CREATE TABLE review_agent.github_app_installation_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    installation_id BIGINT NOT NULL,
    previous_status TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT github_app_installation_events_installation_fk
        FOREIGN KEY (installation_id)
        REFERENCES review_agent.github_app_installations(id),
    CONSTRAINT github_app_installation_events_transition_ck CHECK (
        (previous_status = 'active' AND status IN ('suspended', 'deleted'))
        OR (previous_status = 'suspended' AND status IN ('active', 'deleted'))
    ),
    CONSTRAINT github_app_installation_events_actor_ck CHECK (
        btrim(actor) <> '' AND char_length(actor) <= 120
    ),
    CONSTRAINT github_app_installation_events_reason_ck CHECK (
        btrim(reason) <> '' AND char_length(reason) <= 500
    )
);

CREATE INDEX github_app_installation_events_history_idx
    ON review_agent.github_app_installation_events (
        installation_id, recorded_at DESC, id DESC
    );

CREATE INDEX github_app_repository_access_installation_idx
    ON review_agent.github_app_repository_access (installation_id, repository_id);

CREATE TABLE review_agent.github_app_repository_access_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository_id BIGINT NOT NULL,
    installation_id BIGINT NOT NULL,
    event_kind TEXT NOT NULL,
    access_state TEXT NOT NULL,
    enabled BOOLEAN NOT NULL,
    trigger_mode TEXT NOT NULL,
    profile_key TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT github_app_repository_access_events_repository_fk
        FOREIGN KEY (repository_id) REFERENCES review_agent.repositories(id),
    CONSTRAINT github_app_repository_access_events_installation_fk
        FOREIGN KEY (installation_id)
        REFERENCES review_agent.github_app_installations(id),
    CONSTRAINT github_app_repository_access_events_kind_ck CHECK (
        event_kind IN (
            'granted', 'enabled', 'disabled', 'removed',
            'installation_suspended', 'installation_restored',
            'installation_deleted'
        )
    ),
    CONSTRAINT github_app_repository_access_events_state_ck CHECK (
        access_state IN (
            'available', 'removed', 'installation_suspended',
            'installation_deleted'
        )
    ),
    CONSTRAINT github_app_repository_access_events_trigger_ck
        CHECK (trigger_mode IN ('manual', 'automatic')),
    CONSTRAINT github_app_repository_access_events_profile_ck CHECK (
        profile_key IS NULL
        OR profile_key ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'
    ),
    CONSTRAINT github_app_repository_access_events_actor_ck CHECK (
        btrim(actor) <> '' AND char_length(actor) <= 120
    ),
    CONSTRAINT github_app_repository_access_events_reason_ck CHECK (
        btrim(reason) <> '' AND char_length(reason) <= 500
    )
);

CREATE INDEX github_app_repository_access_events_history_idx
    ON review_agent.github_app_repository_access_events (
        repository_id, recorded_at DESC, id DESC
    );
