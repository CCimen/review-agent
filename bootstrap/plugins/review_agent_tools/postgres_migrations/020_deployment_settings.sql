CREATE TABLE review_agent.deployment_settings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    settings JSONB NOT NULL CHECK (jsonb_typeof(settings) = 'object'),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    reason TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()
);
CREATE TABLE review_agent.service_settings_loads (
    instance_id UUID PRIMARY KEY,
    service TEXT NOT NULL CHECK (service IN ('worker', 'admission', 'reviewer', 'gateway')),
    hostname TEXT NOT NULL,
    revision BIGINT REFERENCES review_agent.deployment_settings(id),
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX service_settings_loads_time_idx ON review_agent.service_settings_loads (loaded_at DESC);
