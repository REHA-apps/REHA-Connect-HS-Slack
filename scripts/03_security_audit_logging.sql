-- scripts/03_security_audit_logging.sql
-- Description: Creates the audit logs table and the 90-day retention pruning logic.

CREATE TABLE IF NOT EXISTS public.audit_logs (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    workspace_id text REFERENCES public.workspaces(id),
    actor_id text, -- e.g. 'slack_user_123' or 'system'
    action text NOT NULL, -- e.g. 'hubspot_install', 'crm_search', 'ticket_claim'
    client_ip text,
    user_agent text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);

-- Index for performance on workspace lookups
CREATE INDEX IF NOT EXISTS idx_audit_logs_workspace_id ON public.audit_logs(workspace_id);
-- Index for performance on action filtering
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON public.audit_logs(action);
-- Index for chronology and effective pruning
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON public.audit_logs(created_at DESC);

-- 1. Enable RLS for security
ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;

-- 2. Log Pruning Function (Retention Policy: 90 Days)
-- This function can be called via RPC to keep the DB size manageable.
CREATE OR REPLACE FUNCTION public.prune_audit_logs(days_to_keep int DEFAULT 90)
 RETURNS int
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    deleted_count int;
BEGIN
    DELETE FROM public.audit_logs
    WHERE created_at < (now() - (days_to_keep || ' days')::interval);

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$function$;
