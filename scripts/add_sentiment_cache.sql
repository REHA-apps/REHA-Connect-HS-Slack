-- C-02: sentiment_cache table
-- Stores SHA-256 hashed text → float sentiment score results
-- from the async Sentinel Lambda so the main Lambda never waits
-- for ML inference.
--
-- Apply via Supabase SQL editor or CLI:
--   supabase db push  (or paste into SQL editor)

CREATE TABLE IF NOT EXISTS sentiment_cache (
    -- SHA-256 of truncated text (first 1000 chars) as hex string
    id          TEXT PRIMARY KEY,
    score       DOUBLE PRECISION NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for TTL sweep (can be run by a scheduled Supabase function to
-- delete rows older than 24 h and keep the table lean).
CREATE INDEX IF NOT EXISTS sentiment_cache_created_at_idx
    ON sentiment_cache (created_at);

-- Row-level security: service role only (match existing Supabase policy pattern)
ALTER TABLE sentiment_cache ENABLE ROW LEVEL SECURITY;
