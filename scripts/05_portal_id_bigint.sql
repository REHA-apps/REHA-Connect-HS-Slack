-- scripts/05_portal_id_bigint.sql
-- Description: Migrates portal_id to BIGINT and identifies object_id as TEXT for HubSpot v3 UUID support.

-- 1. Migrate portal_id in 'workspaces'
-- We use BIGINT to accommodate large HubSpot Hub IDs efficiently.
ALTER TABLE public.workspaces
ALTER COLUMN portal_id TYPE bigint USING portal_id::bigint;

-- 2. Migrate portal_id in 'user_mappings'
ALTER TABLE public.user_mappings
ALTER COLUMN hubspot_owner_id TYPE bigint USING hubspot_owner_id::bigint;

-- 3. Ensure object_id in mappings is TEXT
-- (HubSpot v3 uses string-based UUIDs for engagements and formal objects)
ALTER TABLE public.thread_mappings
ALTER COLUMN object_id TYPE text USING object_id::text;

ALTER TABLE public.ai_scores
ALTER COLUMN object_id TYPE text USING object_id::text;

-- Update the resolve function if it has specific numeric assumptions
-- (Currently delete_workspace_cascade uses TEXT/UUID which is correct).
