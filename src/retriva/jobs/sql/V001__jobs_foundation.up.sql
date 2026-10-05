-- Copyright (C) 2026 Retriva.
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at
--     http://www.apache.org/licenses/LICENSE-2.0
--
-- Retriva Core — Spec 025 (ADR-030): durable job lifecycle foundation.
-- Stream: core.jobs  |  Migration V001: tables, RLS, grants, append-only events.
--
-- Schema adoption: the `jobs` schema is the empty reservation created by
-- CRM V001 ("Durable job tracking (later phases)"), migrator-owned, with
-- CRM-role USAGE and future-table default privileges that the CRM-owned
-- ceding migration (pro.crm V009) removes.  This migration adopts the
-- schema with create-if-absent semantics and forces ownership to the
-- migrator; the final catalog state converges regardless of the valid
-- migration order (Spec 025 §3.13 matrix).
--
-- Tenancy: tenant_id NOT NULL on every table from the first migration;
-- forced RLS keyed on app.current_tenant; fail-closed (an unset context
-- evaluates to NULL, matching no rows) — Constitution §32.
--
-- Event immutability: jobs.job_events is append-only — runtime roles hold
-- no UPDATE/DELETE privileges and a BEFORE UPDATE OR DELETE trigger raises
-- for direct mutations; deletion happens only through the controlled
-- privileged retention path (owner purge via FK cascade at trigger depth
-- > 0, or an explicit privileged transaction that sets
-- app.jobs_privileged_cleanup = 'granted').  Constitution §30.

CREATE SCHEMA IF NOT EXISTS jobs AUTHORIZATION retriva_migrator;
ALTER SCHEMA jobs OWNER TO retriva_migrator;

-- ------------------------------------------------------------------
-- jobs.jobs: one row per logical job
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs.jobs (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT NOT NULL,
    job_type           TEXT NOT NULL,
    payload_version    TEXT NOT NULL,
    status             TEXT NOT NULL
        CHECK (status IN (
            'pending', 'dispatching', 'queued', 'dispatch_unknown',
            'retry_wait', 'running', 'cancelling', 'manual_review',
            'succeeded', 'failed', 'cancelled')),
    subject_type       TEXT,
    subject_id         TEXT,
    input_metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (octet_length(input_metadata::text) <= 16384),
    result_metadata    JSONB
        CHECK (result_metadata IS NULL
               OR octet_length(result_metadata::text) <= 16384),
    progress           INTEGER
        CHECK (progress IS NULL OR (progress >= 0 AND progress <= 100)),
    progress_stage     TEXT,
    progress_message   TEXT
        CHECK (progress_message IS NULL
               OR octet_length(progress_message) <= 512),
    idempotency_key    TEXT,
    requested_by       TEXT,
    queue              TEXT,
    execution_transport TEXT NOT NULL
        CHECK (execution_transport IN ('celery', 'local')),
    scheduled_at       TIMESTAMPTZ,
    submitted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    cancelled_at       TIMESTAMPTZ,
    cancel_requested_at TIMESTAMPTZ,
    attempt_count      INTEGER NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),
    max_attempts       INTEGER NOT NULL DEFAULT 3
        CHECK (max_attempts >= 1),
    last_error_code    TEXT,
    last_error_summary TEXT
        CHECK (last_error_summary IS NULL
               OR octet_length(last_error_summary) <= 512),
    celery_task_id     TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    purge_after        TIMESTAMPTZ
);

-- Idempotency protection: repeating a submission with the same
-- (tenant, job_type, idempotency_key) resolves to the existing job.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_idempotency
    ON jobs.jobs (tenant_id, job_type, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_status_type
    ON jobs.jobs (status, job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant
    ON jobs.jobs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_jobs_purge_after
    ON jobs.jobs (purge_after)
    WHERE purge_after IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_retry_due
    ON jobs.jobs (scheduled_at)
    WHERE status = 'retry_wait';
CREATE INDEX IF NOT EXISTS idx_jobs_celery_task
    ON jobs.jobs (celery_task_id)
    WHERE celery_task_id IS NOT NULL;

-- ------------------------------------------------------------------
-- jobs.job_attempts: one row per durable execution attempt
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs.job_attempts (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL REFERENCES jobs.jobs (id)
                        ON DELETE CASCADE,
    tenant_id           TEXT NOT NULL,
    attempt_no          INTEGER NOT NULL
                        CHECK (attempt_no >= 1),
    dispatch_generation INTEGER NOT NULL DEFAULT 1
                        CHECK (dispatch_generation >= 1),
    dispatch_token      TEXT NOT NULL,
    celery_task_id      TEXT,
    publication_state   TEXT NOT NULL DEFAULT 'prepared'
                        CHECK (publication_state IN (
                            'prepared', 'publishing', 'published',
                            'unknown', 'rejected')),
    published_at        TIMESTAMPTZ,
    publication_tries   INTEGER NOT NULL DEFAULT 0
                        CHECK (publication_tries >= 0),
    execution_generation INTEGER NOT NULL DEFAULT 1
                        CHECK (execution_generation >= 1),
    worker_id           TEXT,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN (
                            'queued', 'running', 'succeeded',
                            'failed', 'lost', 'cancelled',
                            'dispatch_failed')),
    dispatched_at       TIMESTAMPTZ,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    retry_class         TEXT
                        CHECK (retry_class IS NULL OR retry_class IN (
                            'retryable', 'non_retryable',
                            'oom_requeue', 'operator_override',
                            'none')),
    error_code          TEXT,
    error_summary       TEXT
                        CHECK (error_summary IS NULL
                               OR octet_length(error_summary) <= 512),
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb
                        CHECK (octet_length(detail::text) <= 16384),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_job_attempts_no UNIQUE (job_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_job_attempts_live
    ON jobs.job_attempts (status)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_job_attempts_publication
    ON jobs.job_attempts (publication_state)
    WHERE publication_state IN ('prepared', 'publishing', 'unknown');
CREATE INDEX IF NOT EXISTS idx_job_attempts_task
    ON jobs.job_attempts (celery_task_id)
    WHERE celery_task_id IS NOT NULL;

-- ------------------------------------------------------------------
-- jobs.job_events: append-only transition log (Constitution §30)
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs.job_events (
    id          TEXT PRIMARY KEY,
    seq         BIGINT GENERATED ALWAYS AS IDENTITY,
    job_id      TEXT NOT NULL REFERENCES jobs.jobs (id)
                ON DELETE CASCADE,
    tenant_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL
                CHECK (event_type IN (
                    'job_created', 'dispatch_prepared',
                    'dispatch_confirmed', 'dispatch_rejected',
                    'dispatch_ambiguous', 'dispatch_republished',
                    'cancel_requested', 'cancelled',
                    'attempt_claimed', 'attempt_succeeded',
                    'attempt_failed', 'attempt_lost',
                    'retry_scheduled', 'retry_dispatched',
                    'operator_retry', 'operator_resolution',
                    'reconciled', 'anomaly')),
    from_status TEXT,
    to_status   TEXT,
    actor       TEXT NOT NULL
                CHECK (actor IN ('system', 'api', 'worker',
                                 'operator')),
    attempt_id  TEXT,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (octet_length(detail::text) <= 16384),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_seq
    ON jobs.job_events (job_id, seq);

CREATE FUNCTION jobs.forbid_job_event_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'jobs.job_events is append-only: % is forbidden '
                    '(deletion only through the controlled privileged '
                    'retention path)', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$ LANGUAGE plpgsql;

-- Direct UPDATE/DELETE (client- or maintenance-initiated) is refused
-- unless the controlled privileged retention path explicitly grants it
-- for the transaction (app.jobs_privileged_cleanup = 'granted').
-- Cascaded deletions (trigger depth > 0) follow the owning job purge.
CREATE TRIGGER job_events_append_only
    BEFORE UPDATE OR DELETE ON jobs.job_events
    FOR EACH ROW
    WHEN (pg_trigger_depth() = 0
          AND coalesce(current_setting(
              'app.jobs_privileged_cleanup', true), '') <> 'granted')
    EXECUTE FUNCTION jobs.forbid_job_event_mutation();

-- ------------------------------------------------------------------
-- Row-Level Security: tenant isolation + controlled privileged path
-- ------------------------------------------------------------------
ALTER TABLE jobs.jobs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs.job_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs.job_events  ENABLE ROW LEVEL SECURITY;

ALTER TABLE jobs.jobs        FORCE ROW LEVEL SECURITY;
ALTER TABLE jobs.job_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE jobs.job_events  FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON jobs.jobs
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.jobs_privileged_cleanup', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

CREATE POLICY tenant_isolation ON jobs.job_attempts
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.jobs_privileged_cleanup', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

CREATE POLICY tenant_isolation ON jobs.job_events
    FOR ALL
    USING (tenant_id = current_setting('app.current_tenant', true)
           OR current_setting('app.jobs_privileged_cleanup', true)
              = 'granted')
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

-- ------------------------------------------------------------------
-- Runtime grants: retriva_core only (no Pro role receives any
-- privilege on Core job objects; PUBLIC receives nothing)
-- ------------------------------------------------------------------
GRANT USAGE ON SCHEMA jobs TO retriva_core;

GRANT SELECT, INSERT, UPDATE ON jobs.jobs TO retriva_core;
GRANT SELECT, INSERT, UPDATE ON jobs.job_attempts TO retriva_core;
-- job_events is INSERT/SELECT only for the runtime role (append-only).
GRANT SELECT, INSERT ON jobs.job_events TO retriva_core;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA jobs TO retriva_core;

-- Future Core job tables follow the same DML posture; future
-- append-only event tables MUST be granted explicitly without
-- UPDATE/DELETE and MUST carry their own immutability trigger.
ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA jobs
    GRANT SELECT, INSERT, UPDATE ON TABLES TO retriva_core;
ALTER DEFAULT PRIVILEGES FOR ROLE retriva_migrator IN SCHEMA jobs
    GRANT USAGE, SELECT ON SEQUENCES TO retriva_core;

COMMENT ON SCHEMA jobs IS
    'Core-owned durable job lifecycle (core.jobs stream; Spec 025; ADR-030): schema adopted from the CRM V001 reservation';
COMMENT ON TABLE jobs.jobs IS
    'Authoritative logical job lifecycle (PostgreSQL system of record; Celery/Redis remain transport only)';
COMMENT ON TABLE jobs.job_attempts IS
    'Durable execution attempts: preallocated Celery task id, dispatch token/generation, publication state per Spec 025 §3.4';
COMMENT ON TABLE jobs.job_events IS
    'Append-only transition log (privileges + trigger enforced); ordering by seq';
