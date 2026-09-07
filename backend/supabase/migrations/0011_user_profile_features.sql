-- Add columns for new profile features: avatar, and face login integration
-- Se ruleaza dupa 0001-0010, in Supabase SQL Editor.

ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_data TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS face_login_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS face_login_credential TEXT;
