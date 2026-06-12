-- Create scheduled_digests table
CREATE TABLE scheduled_digests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id VARCHAR NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    target_channel VARCHAR NOT NULL,
    cron_expression VARCHAR NOT NULL,
    timezone VARCHAR NOT NULL,
    template_id VARCHAR NOT NULL,
    query_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for querying due digests
CREATE INDEX idx_scheduled_digests_workspace ON scheduled_digests(workspace_id);
