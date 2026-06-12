-- Phase 6.11: Geo-Audit Logging
-- Adds country code column to the audit log trail to leverage Cloudflare IP Geolocation headers.

ALTER TABLE public.audit_logs
ADD COLUMN IF NOT EXISTS country_code CHAR(2) DEFAULT 'XX';

COMMENT ON COLUMN public.audit_logs.country_code IS 'ISO 3166-1 alpha-2 country code provided by Cloudflare (CF-IPCountry). Defaults to XX for unknown/local.';

-- Verify the column exists and show current schema
SELECT column_name, data_type, character_maximum_length, column_default
FROM information_schema.columns
WHERE table_name = 'audit_logs' AND column_name = 'country_code';
