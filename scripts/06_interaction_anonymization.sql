-- scripts/06_interaction_anonymization.sql
-- Description: Establishes the 2026.03 Anonymize-over-Delete protocol and Monthly Slack reporting.

-- 0. Required Extensions (Supabase 2026.03 dependencies)
-- Note: These may require being enabled in the Supabase Dashboard Extensions UI first.
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS http;

-- 1. Create Interaction Logs table (The 'dogfooding' and audit target)
CREATE TABLE IF NOT EXISTS public.interaction_logs (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
    slack_ts text UNIQUE NOT NULL,
    correlation_id text, -- Part of the Triple-Key Trace
    user_id text,
    user_name text,
    message_text text,
    reha_heuristic_score numeric,
    sentiment text,
    is_redacted boolean DEFAULT FALSE,
    redacted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now()
);

-- Indexing for performance and pruning
CREATE INDEX IF NOT EXISTS idx_interaction_logs_workspace_id ON public.interaction_logs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_interaction_logs_slack_ts ON public.interaction_logs(slack_ts);
CREATE INDEX IF NOT EXISTS idx_interaction_logs_created_at ON public.interaction_logs(created_at DESC);

-- 2. Anonymization Trigger Function
-- Instead of hard-deleting, we scrub PII but keep the audit trail intact.
CREATE OR REPLACE FUNCTION public.anonymize_slack_message()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE public.interaction_logs
    SET
        message_text = ' [REDACTED BY USER] ',
        user_name = 'Anonymized',
        user_id = 'redacted',
        is_redacted = TRUE,
        redacted_at = now()
    WHERE slack_ts = OLD.slack_ts;

    RETURN NULL; -- Cancel the actual hard delete
END;
$$ LANGUAGE plpgsql;

-- 3. Bind Trigger to Interaction Logs
DROP TRIGGER IF EXISTS trg_anonymize_interaction ON public.interaction_logs;
CREATE TRIGGER trg_anonymize_interaction
BEFORE DELETE ON public.interaction_logs
FOR EACH ROW
EXECUTE FUNCTION public.anonymize_slack_message();

-- 4. Monthly Performance Summary Logic (Zero-Code HubSpot/Slack BI)
CREATE OR REPLACE FUNCTION public.generate_monthly_reha_summary()
RETURNS void AS $$
DECLARE
    avg_score numeric;
    total_interactions integer;
    webhook_url text := current_setting('app.slack_webhook_url', true);
BEGIN
    -- Enable extensions if not present (requires superuser or predefined permissions)
    -- CREATE EXTENSION IF NOT EXISTS http;
    -- CREATE EXTENSION IF NOT EXISTS pg_cron;

    -- Calculate stats for the last 30 days
    SELECT AVG(reha_heuristic_score), COUNT(*)
    INTO avg_score, total_interactions
    FROM public.interaction_logs
    WHERE created_at > NOW() - INTERVAL '30 days';

    -- Send to Slack via http extension (March 2026.03 Block Kit 2.0 with Table View)
    PERFORM http_post(
        webhook_url,
        jsonb_build_object(
            'text', '📊 *REHA Connect: Monthly Heuristic Summary*',
            'blocks', jsonb_build_array(
                jsonb_build_object(
                    'type', 'header',
                    'text', jsonb_build_object('type', 'plain_text', 'text', 'Monthly Performance Digest (Ireland-DB)')
                ),
                jsonb_build_object(
                    'type', 'section',
                    'fields', jsonb_build_array(
                        jsonb_build_object('type', 'mrkdwn', 'text', '*Total Interactions:*\n' || COALESCE(total_interactions, 0)),
                        jsonb_build_object('type', 'mrkdwn', 'text', '*Avg Heuristic Score:*\n' || ROUND(COALESCE(avg_score, 0), 2))
                    )
                ),
                -- Future expansion: Top 5 Deals Table Block
                jsonb_build_object(
                    'type', 'context',
                    'elements', jsonb_build_array(
                        jsonb_build_object('type', 'mrkdwn', 'text', '💡 _Pruning Policy: 365 Days Anonymized / 90 Days Audit Logs_')
                    )
                )
            )
        )::text,
        'application/json'
    );
END;
$$ LANGUAGE plpgsql;

-- 5. Finalizing the 0-Maintenance Consolidated Cron Job
-- Schedule: First of every month at 3:00 AM
-- Note: Requires pg_cron extension to be active.
SELECT cron.schedule('generate_reha_monthly_digest', '0 3 1 * *', $$
  -- Step 1: Send the Slack Report
  SELECT public.generate_monthly_reha_summary();

  -- Step 2: Prune Interaction Logs older than 1 year (already anonymized by trigger)
  DELETE FROM public.interaction_logs WHERE created_at < NOW() - INTERVAL '1 year';

  -- Step 3: Prune generic Audit Logs older than 90 days
  DELETE FROM public.audit_logs WHERE created_at < NOW() - INTERVAL '90 days';
$$);
