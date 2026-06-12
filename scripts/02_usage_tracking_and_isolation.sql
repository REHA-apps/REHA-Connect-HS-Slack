-- scripts/02_usage_tracking_and_isolation.sql
-- Description: Migrates identity columns to TEXT and adds tier usage tracking metrics.

-- 1. Add Tier Gating & Growth Columns to 'workspaces'
ALTER TABLE public.workspaces
ADD COLUMN IF NOT EXISTS notification_count_monthly integer DEFAULT 0,
ADD COLUMN IF NOT EXISTS total_sync_count integer DEFAULT 0,
ADD COLUMN IF NOT EXISTS last_limit_reset timestamp with time zone,
ADD COLUMN IF NOT EXISTS sent_day4_reminder boolean DEFAULT false;

-- 2. Safe Type Conversion for Identity Columns
-- We use 'USING id::text' to ensure existing UUID or numeric data is safely converted.
ALTER TABLE public.workspaces ALTER COLUMN id TYPE text USING id::text;

ALTER TABLE public.integrations ALTER COLUMN id TYPE text USING id::text;
ALTER TABLE public.integrations ALTER COLUMN workspace_id TYPE text USING workspace_id::text;

ALTER TABLE public.thread_mappings ALTER COLUMN workspace_id TYPE text USING workspace_id::text;

ALTER TABLE public.scoring_configs ALTER COLUMN workspace_id TYPE text USING workspace_id::text;

ALTER TABLE public.ai_scores ALTER COLUMN workspace_id TYPE text USING workspace_id::text;

-- 3. Re-verify the delete cascade function matches the new TEXT types
CREATE OR REPLACE FUNCTION public.delete_workspace_cascade(ws_id text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
    DELETE FROM integrations    WHERE workspace_id = ws_id;
    DELETE FROM thread_mappings WHERE workspace_id = ws_id;
    DELETE FROM scoring_configs WHERE workspace_id = ws_id;
    DELETE FROM ai_scores       WHERE workspace_id = ws_id;
    DELETE FROM user_mappings   WHERE workspace_id = ws_id;
    DELETE FROM workspaces      WHERE id = ws_id;
END;
$function$;
