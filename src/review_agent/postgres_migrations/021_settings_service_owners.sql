ALTER TABLE review_agent.service_settings_loads
    DROP CONSTRAINT service_settings_loads_service_check;
ALTER TABLE review_agent.service_settings_loads
    ADD CONSTRAINT service_settings_loads_service_check
    CHECK (service IN ('worker', 'admission', 'reviewer', 'gateway', 'publisher', 'webhook'));
