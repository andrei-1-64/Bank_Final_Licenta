-- In-app notifications (bell icon in the header). Replaces the Teams
-- webhook for events that happen to an already-logged-in user - Teams stays
-- reserved for password-reset OTP delivery (see core/teams.py), since that
-- flow happens before the user has a session at all.
--
-- Se ruleaza dupa 0001-0009, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL,
    body VARCHAR(500) NOT NULL,
    read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_id_created_at ON notifications (user_id, created_at DESC);

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON notifications TO service_role;
