-- scripts/07_missing_tables.sql
-- Create tables for AI Intent keywords and Ghosting Heartbeats (CR-Hardening)

-- 1. AI Intent Keywords (Skipped: already exists as intent_keywords)

-- 2. Ghosting Heartbeats
CREATE TABLE IF NOT EXISTS ghosting_heartbeats (
    workspace_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    agent_user_id TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    alert_triggered BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (workspace_id, thread_ts)
);

-- Index for background worker lookups
CREATE INDEX IF NOT EXISTS idx_ghosting_heartbeats_expires_at ON ghosting_heartbeats (expires_at) WHERE alert_triggered = FALSE;
