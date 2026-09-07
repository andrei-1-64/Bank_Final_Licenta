-- AI-proposed actions, pending human confirmation.
-- The AI layer NEVER executes write actions directly - it creates a row here.
-- Only the confirm endpoint (app/modules/chat/proposals_service.py), after
-- verifying step-up auth server-side (face token or password), calls the
-- real service function (create_transfer, create_payment, open_account,
-- close_account, cancel_card).
--
-- Se ruleaza dupa 0001-0012, in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status VARCHAR NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'rejected', 'expired')),
    proposal_type VARCHAR NOT NULL,
    payload JSONB NOT NULL,
    summary TEXT NOT NULL,
    result JSONB,
    confirmed_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_proposals_user_id ON proposals (user_id);
CREATE INDEX IF NOT EXISTS idx_proposals_conversation_id ON proposals (conversation_id);
ALTER TABLE proposals ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON proposals TO service_role;
