-- PayProbe Database Schema
-- PostgreSQL 15+

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users and auth
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email         VARCHAR(255) UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    role          VARCHAR(50) NOT NULL DEFAULT 'operator',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Environments
CREATE TABLE environments (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          VARCHAR(100) UNIQUE NOT NULL,
    description   TEXT,
    adapter_configs JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Scenarios
CREATE TABLE scenarios (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          VARCHAR(255) NOT NULL,
    description   TEXT,
    tags          TEXT[] DEFAULT ARRAY[]::TEXT[],
    definition    JSONB NOT NULL,
    created_by    UUID REFERENCES users(id),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    is_active     BOOLEAN DEFAULT TRUE
);

-- Test runs
CREATE TABLE runs (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scenario_ids  UUID[] NOT NULL,
    environment_id UUID REFERENCES environments(id),
    triggered_by  UUID REFERENCES users(id),
    status        VARCHAR(50) NOT NULL DEFAULT 'pending',
    baseline_run_id UUID REFERENCES runs(id),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Phase results
CREATE TABLE run_phases (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase         SMALLINT NOT NULL CHECK (phase IN (1, 2, 3)),
    status        VARCHAR(50) NOT NULL,
    summary       JSONB DEFAULT '{}'::jsonb,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ
);

-- Step results
CREATE TABLE run_steps (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id        UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase_id      UUID REFERENCES run_phases(id),
    scenario_id   UUID REFERENCES scenarios(id),
    step_index    INTEGER NOT NULL,
    step_id       VARCHAR(50),
    target        VARCHAR(100),
    action        VARCHAR(100),
    status        VARCHAR(50) NOT NULL,
    request_payload  JSONB,
    response_payload JSONB,
    duration_ms   INTEGER,
    assertions    JSONB DEFAULT '[]'::jsonb,
    error         TEXT,
    executed_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Baselines
CREATE TABLE baselines (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_id        UUID NOT NULL REFERENCES runs(id),
    label         VARCHAR(255),
    pinned_by     UUID REFERENCES users(id),
    pinned_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common query patterns
CREATE INDEX idx_runs_status      ON runs(status);
CREATE INDEX idx_runs_created     ON runs(created_at DESC);
CREATE INDEX idx_run_steps_run_id ON run_steps(run_id);
CREATE INDEX idx_scenarios_tags   ON scenarios USING gin(tags);
CREATE INDEX idx_scenarios_active ON scenarios(is_active);

-- Seed default environment
INSERT INTO environments (name, description, adapter_configs)
VALUES ('mock', 'Mock environment — all in-memory adapters', '{"mode": "mock"}'::jsonb);
