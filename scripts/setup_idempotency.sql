-- scripts/setup_idempotency.sql
-- Persistent idempotency tracking for webhook events (CR-Marketplace)

CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL, -- 'slack' or 'hubspot'
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL -- Allows for auto-cleanup/TTL
);

-- Index for expiration cleanup
CREATE INDEX IF NOT EXISTS idx_processed_events_expires_at ON processed_events (expires_at);

-- RLS Policy: Only service role can manage (Security Hardening)
ALTER TABLE processed_events ENABLE ROW LEVEL SECURITY;
