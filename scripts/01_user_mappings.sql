-- scripts/01_user_mappings.sql

CREATE TABLE IF NOT EXISTS public.user_mappings (
  id uuid DEFAULT extensions.uuid_generate_v4() PRIMARY KEY,
  workspace_id text NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
  hubspot_owner_id bigint NOT NULL,
  hubspot_email text,
  slack_user_id text,
  mapping_status text DEFAULT 'auto'::text,
  updated_at timestamp with time zone DEFAULT now(),

  -- Ensure one mapping per owner per workspace
  CONSTRAINT user_mappings_workspace_owner_key UNIQUE (workspace_id, hubspot_owner_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_user_mappings_workspace_id ON public.user_mappings USING btree (workspace_id);
CREATE INDEX IF NOT EXISTS idx_user_mappings_owner_id ON public.user_mappings USING btree (hubspot_owner_id);
CREATE INDEX IF NOT EXISTS idx_user_mappings_email ON public.user_mappings USING btree (hubspot_email);

-- Drop the old UUID-based function to avoid overloaded duplicates
DROP FUNCTION IF EXISTS public.delete_workspace_cascade(uuid);

-- Modify the existing delete cascade function (using 'text' for workspace_id)
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
