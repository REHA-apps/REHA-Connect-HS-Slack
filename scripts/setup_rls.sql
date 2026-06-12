-- scripts/setup_rls.sql
-- Defense-in-depth: Enable Row Level Security (RLS) for all tables (Scalability-2)

-- 1. Enable RLS on all tables
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE integrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE thread_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE scoring_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE intent_keywords ENABLE ROW LEVEL SECURITY;
ALTER TABLE ghosting_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE processed_events ENABLE ROW LEVEL SECURITY;

-- 2. Basic Service-Role Access
-- Since the application uses the service_role key for backend operations,
-- we ensure it has bypass privileges or explicit policies.
-- Note: In Supabase, the service_role bypasses RLS by default.
-- These policies are here to prevent accidental exposure if a non-service key is ever used.

-- 3. Tenant Isolation Policies (Example for integrations)
-- Only allow access to records matching the workspace_id if a user JWT was present.
-- For this backend (service-to-service), we primarily rely on service_role bypass.
-- However, we can add a 'restrictive' policy as a fail-safe.

CREATE POLICY service_role_all ON workspaces FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON integrations FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON thread_mappings FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON scoring_configs FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON ai_scores FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON user_mappings FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON intent_keywords FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON ghosting_heartbeats FOR ALL TO service_role USING (true);
CREATE POLICY service_role_all ON processed_events FOR ALL TO service_role USING (true);
